"""pipeline.py: the shape of the OpenFlow pipeline, in one place.

Every switch runs the same four tables, and every rule the controller writes belongs to one of
them at one of the priorities below. Keeping the numbers here means `fortress_ctrl.py` and
`response.py` cannot drift apart, and there is a single page to answer "what are the tables and
why does this rule win?" in the viva.

    table 0  INGRESS   may this port send at all, and which VLAN is it in
    table 1  POLICY    may these two zones talk (T13)
    table 2  QOS       which queue does this traffic belong in (T12)
    table 3  FORWARD   which way to the destination (T08-T11)

Priorities inside a table are what make rules override each other. In table 0, highest first:

    500  BLOCK         this host is cut off (T21) - beats everything below
    200  DROP_IPV6     the lab is IPv4 only
    150  DHCP_GUARD    only srv1's port may send DHCP server replies (T13)
    100  ACCESS/TRUNK  the ordinary "this port may send, here is its VLAN" rule
"""

T_INGRESS, T_POLICY, T_QOS, T_FORWARD = 0, 1, 2, 3

VID_PRESENT = 0x1000      # OpenFlow's "this frame carries a VLAN tag" bit in vlan_vid

PRIO_BLOCK = 500          # T21: a host the threat engine cut off
PRIO_DROP_IPV6 = 200
PRIO_DHCP_GUARD = 150     # T13: rogue DHCP server protection
PRIO_PORT = 100           # the ordinary ingress rule for an access, trunk or switch-link port
PRIO_MONITOR = 60         # T13: the monitoring host is exempt from zone isolation
PRIO_ISOLATION = 50       # T13: zones that may never talk
PRIO_QOS_MISSION = 30     # T12: Command and Communications traffic
PRIO_QOS_BULK = 20        # T12: Barracks traffic
PRIO_FORWARD = 10         # T08: one rule per known destination
