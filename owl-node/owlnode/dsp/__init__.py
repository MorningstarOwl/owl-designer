"""owl-node `dsp` role (the plan's owl-dspd): the designer's inventory, compiler and deployer on the hub.

A design is blocks + wires (the design notes, D1). Device blocks come from the hub's PipeWire graph and from
the NMOS registry (every owl-node's senders and receivers); processing blocks come from dsp/blocks/*.yaml. The
compiler turns wires into PipeWire links and IS-05 patches, the deployer applies them with a diff and rollback and
re-asserts them after restarts, and the meters tap signal for Run mode and the health check.
"""
