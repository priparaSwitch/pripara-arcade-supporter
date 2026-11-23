Application-only native source

sender/PriParaFrameSender.cpp is the independent Windows x64 RGB frame sender.
scripts/build_sender.py rebuilds this one DLL and places it in app/native.
The public C ABI is PPF_Version, PPF_Create, PPF_SendRgb and PPF_Close.
sender/selftest_receiver.cpp is an isolated original-protocol receiver used
only by build verification. Its temporary test binaries accept channels 50/51
and reject installed camera channels 0/1. They are not runtime app products.

qr/shared.inl preserves the published UnityCapture shared-memory protocol
reference used by the sender. It is not compiled into the application sender.
Its copyright and MIT permission notice are preserved in qr/LICENSE.txt.
The upstream source reference is recorded in the sender's header.

This source package contains no upper-camera driver source/build target,
filter-registration command, driver installer or installed DLL build input.
The existing PriPara QR Camera driver is a separately installed dependency.

The builder applies compiler path maps and normalizes only standalone source
filename strings in the PE read-only .rdata section, preserving string storage
length, image layout, code and relocation bytes. This removes CI build paths
from precompiled static C++ runtime diagnostics. The same normalization is
applied to the test sender before its protocol check.
