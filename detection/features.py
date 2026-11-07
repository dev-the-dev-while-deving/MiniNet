"""features.py: the names of the traffic features, and nothing else.

Four programs need this list and they don't all run on the same Python: the collector and the
model use the fortress venv (scapy, scikit-learn), while the topology and its health checks use
the system Python that Mininet lives in. Keeping the list in a module with no imports means
everyone can agree on it without dragging scapy or sklearn along.
"""

# Columns the collector writes (detection/flow_collector.py, T18).
FIELDS = ["time", "host", "ip", "zone", "label", "packets_per_s", "bytes_per_s", "new_conns_per_s",
          "distinct_dst_ports", "distinct_dst_hosts", "failed_handshakes"]

# The numeric ones: what the Isolation Forest is trained on (T19) and what the threat engine
# quotes in its reasons (T20). The order matters: it is the order of the model's input.
FEATURES = FIELDS[5:]
