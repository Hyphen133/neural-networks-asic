# Makefile for the tt_um_mnist_nn cocotb testbench.
#   make            -- RTL simulation
#   GATES=yes make  -- gate-level simulation of runs/wokwi/results/final/verilog/gl/*.v

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = tt_um_mnist_nn.sv

ifneq ($(GATES),yes)

# RTL simulation
SIM_BUILD        = sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
COMPILE_ARGS    += -g2012

# Optional parameter overrides, e.g.  HIDDEN=8 make   /   SHIFT_FIXED=6 make
# (test.py reads $HIDDEN too, so the golden model follows.)
ifneq ($(HIDDEN),)
COMPILE_ARGS    += -Ptb.user_project.HIDDEN=$(HIDDEN)
endif
ifneq ($(MULT_TERNARY),)
COMPILE_ARGS    += -Ptb.user_project.MULT_TERNARY=$(MULT_TERNARY)
endif
ifneq ($(SHIFT_FIXED),)
COMPILE_ARGS    += -Ptb.user_project.SHIFT_FIXED=$(SHIFT_FIXED)
endif
ifneq ($(ACC_W),)
COMPILE_ARGS    += -Ptb.user_project.ACC_W=$(ACC_W)
endif

else

# Gate-level simulation
SIM_BUILD        = sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DUSE_POWER_PINS
COMPILE_ARGS    += -DUNIT_DELAY=\#1
COMPILE_ARGS    += -g2012
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell.v
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

VERILOG_SOURCES += $(PWD)/tb.v

TOPLEVEL = tb
MODULE   = test

include $(shell cocotb-config --makefiles)/Makefile.sim
