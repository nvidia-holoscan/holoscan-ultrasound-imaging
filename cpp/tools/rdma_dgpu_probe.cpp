// Bounded NIC loopback commissioning check; never opens or fires the HSB.
#include <cuda.h>
#include <infiniband/verbs.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>
#include <algorithm>
#include <chrono>
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
void require(bool ok, const std::string &message) {
  if (!ok) throw std::runtime_error(message);
}
void cuda_check(CUresult result, const char *operation) {
  const char *message = nullptr;
  cuGetErrorString(result, &message);
  require(result == CUDA_SUCCESS,
          std::string(operation) + ": " + (message ? message : "unknown"));
}
void verbs_check(int result, const char *operation) {
  require(!result, std::string(operation) + ": " + std::strerror(result));
}
struct Resources {
  CUdevice device = 0;
  CUcontext cuda = nullptr;
  CUdeviceptr allocation = 0, gpu = 0;
  int fd = -1;
  ibv_context *nic = nullptr;
  ibv_pd *pd = nullptr;
  ibv_cq *cq = nullptr;
  ibv_qp *sender = nullptr, *receiver = nullptr;
  ibv_mr *source_mr = nullptr, *host_mr = nullptr, *gpu_mr = nullptr;
  std::vector<uint8_t> source;
  std::unique_ptr<uint8_t, decltype(&std::free)> host{nullptr, &std::free};
  void release() {
    ibv_qp_attr error{};
    error.qp_state = IBV_QPS_ERR;
    for (auto qp : {sender, receiver})
      if (qp) verbs_check(ibv_modify_qp(qp, &error, IBV_QP_STATE), "QP ERR");
    if (cq) for (int i = 0; i < 64; ++i) {
      ibv_wc completion{};
      int count = ibv_poll_cq(cq, 1, &completion);
      require(count >= 0, "drain CQ failed");
      if (!count) break;
    }
    if (nic) {
      int flags = fcntl(nic->async_fd, F_GETFL);
      require(flags >= 0 && !fcntl(nic->async_fd, F_SETFL, flags | O_NONBLOCK),
              "configure async fd failed");
      for (int i = 0; i < 100; ++i) {
        ibv_async_event event{};
        if (ibv_get_async_event(nic, &event)) break;
        ibv_ack_async_event(&event);
      }
    }
#define DROP(resource, function) if (resource) { verbs_check(function(resource), #function); resource = nullptr; }
    DROP(sender, ibv_destroy_qp); DROP(receiver, ibv_destroy_qp);
    DROP(source_mr, ibv_dereg_mr); DROP(host_mr, ibv_dereg_mr); DROP(gpu_mr, ibv_dereg_mr);
    DROP(cq, ibv_destroy_cq); DROP(pd, ibv_dealloc_pd); DROP(nic, ibv_close_device);
#undef DROP
    if (fd >= 0) { require(!::close(fd), "close DMA-BUF failed"); fd = -1; }
    if (allocation) { cuda_check(cuMemFree(allocation), "free GPU ring"); allocation = gpu = 0; }
    if (cuda) { cuda_check(cuDevicePrimaryCtxRelease(device), "release CUDA context"); cuda = nullptr; }
  }
  ~Resources() {
    try { release(); } catch (...) {
      // A failed NIC teardown must not release memory it may still map.
      auto leaked_source = new std::vector<uint8_t>;
      leaked_source->swap(source);
      host.release(); allocation = 0; cuda = nullptr;
    }
  }
};
void connect(ibv_qp *local, ibv_qp *peer, const ibv_gid &gid, unsigned index, bool uc) {
  ibv_qp_attr attr{};
  attr.qp_state = IBV_QPS_INIT;
  attr.pkey_index = 0;
  attr.port_num = 1;
  attr.qp_access_flags = IBV_ACCESS_REMOTE_WRITE;
  verbs_check(ibv_modify_qp(local, &attr, IBV_QP_STATE | IBV_QP_PKEY_INDEX |
                           IBV_QP_PORT | IBV_QP_ACCESS_FLAGS), "QP INIT");
  attr = {};
  attr.qp_state = IBV_QPS_RTR;
  attr.path_mtu = IBV_MTU_1024;
  attr.dest_qp_num = peer->qp_num;
  attr.rq_psn = 0;
  attr.max_dest_rd_atomic = 1;
  attr.min_rnr_timer = 12;
  attr.ah_attr.is_global = 1;
  attr.ah_attr.port_num = 1;
  attr.ah_attr.grh.dgid = gid;
  attr.ah_attr.grh.sgid_index = index;
  attr.ah_attr.grh.hop_limit = 1;
  int flags = IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN | IBV_QP_RQ_PSN;
  if (!uc) flags |= IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER;
  verbs_check(ibv_modify_qp(local, &attr, flags), "QP RTR");
  attr = {};
  attr.qp_state = IBV_QPS_RTS;
  attr.timeout = 14;
  attr.retry_cnt = 3;
  attr.rnr_retry = 3;
  attr.sq_psn = 0;
  attr.max_rd_atomic = 1;
  flags = IBV_QP_STATE | IBV_QP_SQ_PSN;
  if (!uc) flags |= IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY | IBV_QP_MAX_QP_RD_ATOMIC;
  verbs_check(ibv_modify_qp(local, &attr, flags), "QP RTS");
}
void transfer(Resources &r, ibv_mr *destination, size_t bytes, size_t offset) {
  ibv_recv_wr receive{}, *bad_receive = nullptr;
  receive.wr_id = 2;
  verbs_check(ibv_post_recv(r.receiver, &receive, &bad_receive), "post receive");
  ibv_sge segment{};
  segment.addr = reinterpret_cast<uintptr_t>(r.source.data());
  segment.length = bytes;
  segment.lkey = r.source_mr->lkey;
  ibv_send_wr send{}, *bad_send = nullptr;
  send.wr_id = 1;
  send.sg_list = &segment;
  send.num_sge = 1;
  send.opcode = IBV_WR_RDMA_WRITE_WITH_IMM;
  send.send_flags = IBV_SEND_SIGNALED;
  send.imm_data = htonl(0x1234);
  // Match the SDK's DMA-BUF mapping: IOVA zero, not a CUDA virtual address.
  send.wr.rdma.remote_addr = offset;
  send.wr.rdma.rkey = destination->rkey;
  verbs_check(ibv_post_send(r.sender, &send, &bad_send), "post RDMA write");
  bool sent = false, received = false;
  auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
  while (!sent || !received) {
    ibv_wc completion{};
    int count = ibv_poll_cq(r.cq, 1, &completion);
    require(count >= 0, "poll CQ failed");
    if (count) {
      require(completion.status == IBV_WC_SUCCESS,
              std::string("CQ failure: ") + ibv_wc_status_str(completion.status) +
              " vendor=" + std::to_string(completion.vendor_err));
      if (completion.wr_id == 1) sent = true;
      else if (completion.wr_id == 2) {
        require(completion.wc_flags & IBV_WC_WITH_IMM, "missing immediate completion");
        require(ntohl(completion.imm_data) == 0x1234, "wrong immediate completion");
        received = true;
      } else require(false, "unexpected completion");
    }
    require(std::chrono::steady_clock::now() < deadline, "RDMA completion timeout");
    if (!count) std::this_thread::yield();
  }
}
int run(const char *device_name, unsigned rounds, bool uc) {
  Resources r;
  cuda_check(cuInit(0), "cuInit");
  cuda_check(cuDeviceGet(&r.device, 0), "cuDeviceGet");
  cuda_check(cuDevicePrimaryCtxRetain(&r.cuda, r.device), "retain CUDA context");
  cuda_check(cuCtxSetCurrent(r.cuda), "set CUDA context");
  char name[256]{};
  cuda_check(cuDeviceGetName(name, sizeof(name), r.device), "GPU name");
  int integrated = 0, supported = 0, ordering = 0, options = 0;
  cuda_check(cuDeviceGetAttribute(&integrated, CU_DEVICE_ATTRIBUTE_INTEGRATED, r.device), "integrated");
  require(!integrated, "Select a discrete GPU with CUDA_VISIBLE_DEVICES");
  cuda_check(cuDeviceGetAttribute(&supported, CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED, r.device), "DMA-BUF support");
  require(supported, "GPU does not support DMA-BUF");
  cuda_check(cuDeviceGetAttribute(&ordering, CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_WRITES_ORDERING, r.device), "RDMA ordering");
  cuda_check(cuDeviceGetAttribute(&options, CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_FLUSH_WRITES_OPTIONS, r.device), "RDMA flush options");
  require(ordering >= CU_GPU_DIRECT_RDMA_WRITES_ORDERING_OWNER ||
          (options & CU_FLUSH_GPU_DIRECT_RDMA_WRITES_OPTION_HOST), "No supported host visibility fence");
  std::cout << "GPU=" << name << " NIC=" << device_name << " ordering=" << ordering
            << " flush_options=" << options << " rounds=" << rounds << std::endl;
  std::cout << "NIC-local " << (uc ? "UC" : "RC")
            << " loopback; does not validate external wire or FPGA" << std::endl;
  int count = 0;
  auto devices = ibv_get_device_list(&count);
  require(devices, "cannot enumerate RDMA devices");
  for (int i = 0; i < count; ++i)
    if (std::string(ibv_get_device_name(devices[i])) == device_name)
      r.nic = ibv_open_device(devices[i]);
  ibv_free_device_list(devices);
  require(r.nic, "cannot open selected NIC");
  ibv_port_attr port{};
  verbs_check(ibv_query_port(r.nic, 1, &port), "query port");
  require(port.state == IBV_PORT_ACTIVE, "NIC port is not active");
  ibv_gid gid{};
  unsigned gid_index = 0;
  bool found = false;
  for (int i = 0; i < port.gid_tbl_len; ++i) {
    ibv_gid_entry entry{};
    if (!ibv_query_gid_ex(r.nic, 1, i, &entry, 0) &&
        entry.gid_type == IBV_GID_TYPE_ROCE_V2 &&
        entry.gid.raw[10] == 0xff && entry.gid.raw[11] == 0xff) {
      gid = entry.gid;
      gid_index = i;
      found = true;
      break;
    }
  }
  require(found, "no IPv4 RoCE-v2 GID");
  std::cout << "gid_index=" << gid_index << std::endl;
  constexpr size_t rf_bytes = 285504, metadata_offset = 285568, page_bytes = 285696;
  constexpr size_t ring_pages = 1024, region_bytes = page_bytes * ring_pages;
  r.source.resize(rf_bytes);
  // IOVA zero requires the CPU address to have the same page offset: zero.
  void *host_pointer = nullptr;
  verbs_check(posix_memalign(&host_pointer, 4096, region_bytes), "allocate host ring");
  r.host.reset(static_cast<uint8_t *>(host_pointer));
  r.pd = ibv_alloc_pd(r.nic);
  require(r.pd, "allocate PD failed");
  r.cq = ibv_create_cq(r.nic, 16, nullptr, nullptr, 0);
  require(r.cq, "create CQ failed");
  r.source_mr = ibv_reg_mr(r.pd, r.source.data(), r.source.size(), IBV_ACCESS_LOCAL_WRITE);
  require(r.source_mr, std::string("register source MR: ") + std::strerror(errno));
  r.host_mr = ibv_reg_mr_iova(r.pd, r.host.get(), region_bytes, 0,
                             IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);
  require(r.host_mr, std::string("register host destination MR: ") + std::strerror(errno));
  cuda_check(cuMemAlloc(&r.allocation, region_bytes + 65536), "allocate GPU ring");
  r.gpu = (r.allocation + 4095) & ~CUdeviceptr(4095);
  cuda_check(cuMemGetHandleForAddressRange(&r.fd, r.gpu, region_bytes,
             CU_MEM_RANGE_HANDLE_TYPE_DMA_BUF_FD, 0), "export DMA-BUF");
  r.gpu_mr = ibv_reg_dmabuf_mr(r.pd, 0, region_bytes, 0, r.fd,
                               IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);
  require(r.gpu_mr, std::string("register GPU DMA-BUF MR: ") + std::strerror(errno));
  std::cout << "DMA-BUF ring bytes=" << region_bytes << " pages=" << ring_pages
            << " offset=0 iova=0 mr_addr=" << r.gpu_mr->addr
            << " mr_length=" << r.gpu_mr->length << std::endl;
  ibv_qp_init_attr init{};
  init.send_cq = init.recv_cq = r.cq;
  init.qp_type = uc ? IBV_QPT_UC : IBV_QPT_RC;
  init.cap.max_send_wr = init.cap.max_recv_wr = 8;
  init.cap.max_send_sge = init.cap.max_recv_sge = 1;
  r.sender = ibv_create_qp(r.pd, &init);
  r.receiver = ibv_create_qp(r.pd, &init);
  require(r.sender && r.receiver, "create QP failed");
  connect(r.sender, r.receiver, gid, gid_index, uc);
  connect(r.receiver, r.sender, gid, gid_index, uc);
  struct Case { size_t bytes, offset; };
  const Case cases[] = {
      {64, 0},
      {rf_bytes, 0}, {128, metadata_offset},
      {rf_bytes, 3 * page_bytes}, {128, 3 * page_bytes + metadata_offset},
      {rf_bytes, (ring_pages - 1) * page_bytes},
      {128, (ring_pages - 1) * page_bytes + metadata_offset}};
  std::vector<uint8_t> actual(rf_bytes);
  size_t failures = 0;
  for (unsigned mode = 0; mode < 3; ++mode) {
    size_t bad = 0, checks = 0;
    for (unsigned round = 0; round < rounds; ++round)
      for (unsigned c = 0; c < sizeof(cases) / sizeof(cases[0]); ++c) {
        auto item = cases[c];
        for (size_t i = 0; i < item.bytes; ++i)
          r.source[i] = uint8_t(i * 17 + round * 31 + c * 7);
        if (mode) {
          cuda_check(cuMemsetD8(r.gpu + item.offset, 0xa5, item.bytes), "clear GPU destination");
          cuda_check(cuCtxSynchronize(), "finish destination clear");
        } else std::fill_n(r.host.get() + item.offset, item.bytes, 0xa5);
        transfer(r, mode ? r.gpu_mr : r.host_mr, item.bytes, item.offset);
        if (mode == 2)
          cuda_check(cuFlushGPUDirectRDMAWrites(CU_FLUSH_GPU_DIRECT_RDMA_WRITES_TARGET_CURRENT_CTX,
                     CU_FLUSH_GPU_DIRECT_RDMA_WRITES_TO_OWNER), "RDMA visibility flush");
        if (mode)
          cuda_check(cuMemcpyDtoH(actual.data(), r.gpu + item.offset, item.bytes), "read GPU result");
        else std::copy_n(r.host.get() + item.offset, item.bytes, actual.data());
        auto mismatch = std::mismatch(r.source.begin(), r.source.begin() + item.bytes, actual.begin());
        ++checks;
        if (mismatch.first != r.source.begin() + item.bytes) {
          if (bad < 5) std::cout << "mismatch mode=" << mode << " round=" << round
            << " bytes=" << item.bytes << " offset=" << item.offset
            << " first_bad=" << (mismatch.first - r.source.begin())
            << " expected=" << unsigned(*mismatch.first)
            << " actual=" << unsigned(*mismatch.second) << std::endl;
          ++bad;
        }
      }
    std::cout << "RESULT mode=" << (mode == 0 ? "host_control" : mode == 1 ? "gpu_no_flush" : "gpu_flush")
              << " checks=" << checks << " mismatches=" << bad << std::endl;
    failures += bad;
  }
  r.release();
  return failures ? 2 : 0;
}
} // namespace
int main(int argc, char **argv) {
  try {
    require(argc >= 2 && argc <= 4, "Usage: rdma_dgpu_probe NIC [rounds:1..256] [rc|uc]");
    unsigned rounds = argc >= 3 ? std::stoul(argv[2]) : 32;
    require(rounds >= 1 && rounds <= 256, "rounds must be 1..256");
    std::string protocol = argc == 4 ? argv[3] : "rc";
    require(protocol == "rc" || protocol == "uc", "protocol must be rc or uc");
    return run(argv[1], rounds, protocol == "uc");
  } catch (const std::exception &error) {
    std::cerr << "FAIL: " << error.what() << std::endl;
    return 1;
  }
}
