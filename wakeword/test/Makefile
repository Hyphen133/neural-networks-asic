# cocotb testbench for tt_um_wakeword.
#   make                    RTL, short frames (fast)
#   FRAME_LOG2=16 make      full-rate frames (slow)
#   GATES=yes make          gate-level

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src

FRAME_LOG2 ?= 8
NBAND      ?= 5

ifneq ($(GATES),yes)
SIM_BUILD        = sim_build/rtl
VERILOG_SOURCES += $(SRC_DIR)/tt_um_wakeword.sv
COMPILE_ARGS    += -g2012 -I$(SRC_DIR)
COMPILE_ARGS    += -DWW_FRAME_LOG2=$(FRAME_LOG2)
ifneq ($(NBAND),)
COMPILE_ARGS    += -DWW_NBAND=$(NBAND)
endif
ifneq ($(NHID),)
COMPILE_ARGS    += -DWW_NHID=$(NHID)
endif
else
SIM_BUILD        = sim_build/gl
COMPILE_ARGS    += -DGL_TEST -DFUNCTIONAL -DUSE_POWER_PINS -DUNIT_DELAY=\#1 -g2012
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell.v
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v
endif

VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb
MODULE   ?= test

export FRAME_LOG2
include $(shell cocotb-config --makefiles)/Makefile.sim
