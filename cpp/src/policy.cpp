#include "nvus/policy.hpp"
#include <algorithm>
#include <cmath>

namespace nvus {
Pw4cmPolicy::Pw4cmPolicy(std::vector<double> angles, std::string label)
    : angles_(std::move(angles)), label_(std::move(label)) {
  if (angles_.empty() || label_.empty())
    throw std::invalid_argument("PW4cm angles and label are required");
  for (double angle : angles_)
    if (!std::isfinite(angle) || std::abs(angle) >= 90.)
      throw std::invalid_argument("PW4cm angles must be finite and inside +/-90 degrees");
}

std::vector<TransmitEvent>
Pw4cmPolicy::next(size_t count, const PolicyObservation &previous) const {
  if (!count) throw std::invalid_argument("Policy next count must be positive");
  std::vector<TransmitEvent> result;
  result.reserve(count);
  for (size_t i = 0; i < count; ++i) {
    const auto absolute = previous.next_event + i;
    const auto index = size_t(absolute % angles_.size());
    result.push_back({angles_[index], absolute / angles_.size(), index,
                      angles_.size(), label_});
  }
  return result;
}

CompositePolicy::CompositePolicy(std::shared_ptr<Pw4cmPolicy> first,
                                 std::shared_ptr<Pw4cmPolicy> second,
                                 Composition composition)
    : first_(std::move(first)), second_(std::move(second)),
      composition_(composition) {
  if (!first_ || !second_ || first_->angles().size() != second_->angles().size())
    throw std::invalid_argument("Composed policies require equal angle counts");
}

std::vector<TransmitEvent>
CompositePolicy::next(size_t count, const PolicyObservation &previous) const {
  if (!count) throw std::invalid_argument("Policy next count must be positive");
  const auto width = first_->angles().size();
  std::vector<TransmitEvent> result;
  for (size_t i = 0; i < count; ++i) {
    const auto absolute = previous.next_event + i;
    const auto cycle = absolute / (2 * width);
    const auto within = size_t(absolute % (2 * width));
    const bool second = composition_ == Composition::Interleave
                            ? within % 2 : within >= width;
    const auto index = composition_ == Composition::Interleave
                           ? within / 2 : within % width;
    const auto &source = second ? second_ : first_;
    PolicyObservation local;
    local.next_event = cycle * width + index;
    auto event = source->next(1, local).front();
    event.group_id = cycle * 2 + second;
    event.group_label = second ? "B" : "A";
    result.push_back(std::move(event));
  }
  return result;
}

std::shared_ptr<Policy> interleave(std::shared_ptr<Pw4cmPolicy> a,
                                   std::shared_ptr<Pw4cmPolicy> b) {
  return std::make_shared<CompositePolicy>(std::move(a), std::move(b),
                                           Composition::Interleave);
}
std::shared_ptr<Policy> stack(std::shared_ptr<Pw4cmPolicy> a,
                              std::shared_ptr<Pw4cmPolicy> b) {
  return std::make_shared<CompositePolicy>(std::move(a), std::move(b),
                                           Composition::Stack);
}
} // namespace nvus
