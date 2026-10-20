# Hardware shopping list

Parts for the Holoscan Ultrasound Imaging hardware reference architecture.

> [!WARNING]
> ⚠️ Dangerous voltages: appropriate electrical and high-voltage safety training is
> required. The EVM pulser supports up to ±100 V; the Sonosray L12-5 D probe
> is rated to ±50 V, per the manufacturer. Do not exceed the probe rating.
> These are limits, not operating settings. Limit each bench supply to at most 100 mA as defined by the EVM documentation. This reference
> limits each supply to 20–30 mA. Verify voltage limits, current limits and series-operation
> ratings against the EVM, probe and supply documentation before use.

Nominal settings used for this work: **±15 V** on the TX rails, with each
supply limited to **20–30 mA**.

For more information about operating voltage and amperage limits, see the
[TI AFE5932EVM user guide and resources](https://www.ti.com/tool/AFE5932EVM).

This list describes an initial implementation, not an endorsement of product quality or suitability. Users should assess parts for their application.

| Item | Quantity | Part / supplier | Action | Ordering notes |
| --- | --- | --- | --- | --- |
| GPU host | 1 | Lenovo ThinkStation PGX or NVIDIA DGX Spark, each with integrated ConnectX-7 NIC | ThinkStation PGX [(buy)](https://www.lenovo.com/buy/us/en/thinkstation-commercial-computers-0avz00a); DGX Spark [(buy)](https://marketplace.nvidia.com/en-us/enterprise/personal-ai-supercomputers/dgx-spark/) | Choose (at least) one. |
| Holoscan Sensor Bridge FPGA board | 1 | Terasic Stratix 10 Sensor Processing Kit, P0817 / S10-Holoscan; FPGA 1ST040EH2F35E2VG; 12 V DC input | [(buy)](https://www.terasic.com.tw/cgi-bin/page/archive.pl?Language=English&No=1340) | |
| Ultrasound frontend | 1 | TI AFE5932EVM, including AFE5932 receivers and TX73L64 transmitters | [(buy)](https://www.ti.com/product/AFE5932EVM/part-details/AFE5932EVM?s=0e52e922150v17550f&login-check=true) | |
| Probe | 1 | Sonosray L12-5 D | [(catalogue)](http://www.sonosray.com/en/Products/list.aspx?lcid=17&pid=50); [(request)](mailto:seven.wang@sonosray.com?subject=L12-5%20D%20probe%20inquiry&body=Hello%20Seven%2C%0A%0AI%20would%20like%20to%20inquire%20about%20the%20Sonosray%20L12-5%20D%20probe%20for%20the%20Holoscan%20Ultrasound%20Imaging%20reference%20architecture.%0A%0AThank%20you.) | |
| Probe adapter | 1 | Probe-to-AFE5932EVM adapter | TI [(request)](mailto:sanjayrpithadia@ti.com?subject=AFE5932EVM%20probe%20adapter%20request&body=Hello%20Sanjay%2C%0A%0AI%20would%20like%20to%20request%20a%20probe-to-AFE5932EVM%20adapter%20for%20the%20Holoscan%20Ultrasound%20Imaging%20reference%20architecture.%0A%0AThank%20you.) | Expected at no charge; confirm when ordering. |
| FPGA programmer | 1, optional | Altera USB-Blaster II, PL-USB2-BLASTER | [(buy)](https://www.digikey.com/en/products/detail/altera/PL-USB2-BLASTER/5638362) | |
| Host-to-FPGA cable | 1 | FS QSFP-100G-PC01, product 72702; 1 m, 100G QSFP28 passive DAC, NVIDIA/Mellanox compatible | [(buy)](https://www.fs.com/uk/products/72702.html) | |
| TX high-voltage supplies | 2 | Isolated, current-limited bench supplies rated for series operation | | Nominal setting used for this work: ±15 V, with each supply set to 15 V and limited to 20–30 mA. Select supplies suitable for this operating point, connected in series for positive and negative rails; qualify substitutes before use. |
| AFE low-voltage supply | 1 assembly | Custom-built ±5 V supply using two wall adapters | | |
| Board power leads | 1 set | PHV, MHV, ground and low-voltage leads | | High-voltage leads are often included with bench supplies; check kit contents and board connector compatibility. |
| Display and controls | 1 set | Monitor, video cable, keyboard and mouse | | |

For any questions, [please open an issue](https://github.com/nvidia-holoscan/holoscan-ultrasound-imaging/issues).

### FAQ:
