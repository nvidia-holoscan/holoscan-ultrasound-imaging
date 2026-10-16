# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NVUS/TI-EVM ultrasound stack: driver, wire-format decode, unit converters.

Acquisition uses the native PolicyExecutor; reconstruction imports CuPy on demand.
"""

import importlib


def __getattr__(name):
    # Version/help and dependency checks must work without NumPy or CuPy.
    if name in ("afe", "bus", "fpga", "raw", "tx", "units", "recon"):
        return importlib.import_module("." + name, __name__)
    if name == "Trace":
        return importlib.import_module(".bus", __name__).Trace
    if name in ("Evm", "EvmConfig", "enumeration_strategy"):
        return getattr(importlib.import_module(".evm", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
