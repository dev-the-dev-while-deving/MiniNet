# Cisco equivalents

Most networking courses (and most examiners) think in Cisco IOS terms. This table maps every
part of Fortress-Net to what you would configure on Cisco equipment, and where ours lives.
The protocols are the same open standards; only the software that speaks them differs.

| What it does | Cisco equivalent | Fortress-Net | Where |
|--------------|------------------|--------------|-------|
| Switches with VLANs | Catalyst access switch, `switchport access vlan 10` | Open vSwitch; the controller pushes/pops the 802.1Q tag per port (D10) | `controller/vlan.py` |
| Trunk to a router | `switchport mode trunk` | router ports carry tagged frames; sub-interfaces `r1-eth0.10` | `topology/fortress_topo.py` |
| Router-on-a-stick | `interface g0/0.10`, `encapsulation dot1Q 10` | Linux VLAN sub-interfaces on r1/r2 | `topology/fortress_topo.py` (configure_routers) |
| Loop prevention | Spanning Tree (PVST+/RSTP) | RSTP before the controller (D11); then controller-computed loop-free trees (D21) | `controller/paths.py` |
| EtherChannel / LACP | `channel-group 1 mode active` | two c1-c2 cables in an OpenFlow **select group** (D24) | `controller/fortress_ctrl.py` |
| Fast reroute | Flex Links, or MPLS/IP FRR | OpenFlow **fast-failover groups** (primary + backup port) (D25) | `controller/fortress_ctrl.py`, `--mode proactive` |
| Central network control | Cisco DNA Center / ACI (APIC) | Ryu SDN controller with a REST API | `controller/` |
| Dynamic routing | `router ospf 1`, `network ... area 0` | FRRouting `ospfd`, area 0, point-to-point links, 1 s hello / 4 s dead | `configs/frr/frr.conf.j2` |
| Choosing the primary path | `ip ospf cost 10` | same command in FRR: r1-r3 cost 10, r2-r3 cost 100 | `config/fortress.yaml` |
| Backup static route | `ip route 172.16.70.0 255.255.255.0 10.0.0.10 250` (floating static) | identical FRR line on r2 | `configs/frr/frr.conf.j2` |
| Loopback interface | `interface Loopback0`, `ip address 1.1.1.1 255.255.255.255` | `interface lo` / `ip address 1.1.1.1/32`, advertised in OSPF (D39) | `configs/frr/frr.conf.j2` |
| Gateway redundancy | HSRP (`standby 10 ip 192.168.10.1`) or VRRP (`vrrp 10 ip ...`) | FRR `vrrpd`, VRRPv3, priority 120/100, preempt, 200 ms adverts (D19) | `configs/frr/frr.conf.j2` |
| NAT / PAT | `ip nat inside source list 1 interface g0/1 overload` | `iptables -t nat ... -j MASQUERADE` on the ISP link, plus a NAT exemption for lab addresses | `configs/iptables/rules.j2` |
| Access lists between zones | extended ACLs / zone-based firewall | controller drops never-talk zone pairs; stateful `iptables` FORWARD rules on r1/r2 (D27) | `controller/fortress_ctrl.py`, `configs/iptables/rules.j2` |
| Port security | `switchport port-security mac-address sticky` | only the host's own MAC + IP accepted on its port (table 0) | `controller/fortress_ctrl.py` |
| DHCP snooping | `ip dhcp snooping`, trusted port | DHCP answers only accepted from srv1's port (DHCP guard) | `controller/fortress_ctrl.py` |
| DHCP server and relay | `ip dhcp pool`, `ip helper-address` | dnsmasq on srv1; `dhcrelay` on r1/r2 | `configs/dnsmasq/` |
| Site-to-site VPN | crypto map / IKEv2 + IPsec tunnel, `tunnel source Loopback0` | strongSwan IKEv2, AES-256-GCM ESP, tunnel mode, between loopbacks (D28, D39) | `topology/render_configs.py` (strongswan), `topology/vpn.py` |
| VPN keepalive | IKE DPD (`crypto isakmp keepalive`) | strongSwan `dpd_delay = 1s` | same |
| QoS | MQC: `class-map`, `policy-map`, `priority`/`bandwidth` | Linux HTB queues on OVS ports; the controller picks the queue (D26) | `controller/fortress_ctrl.py`, `config/fortress.yaml` (qos) |
| SPAN (port mirroring) | `monitor session 1 source ...` | OVS mirror on both cores to ids1 (D30) | `topology/fortress_topo.py` |
| IDS/IPS | Cisco Firepower / Snort | Suricata with our own rules | `detection/suricata/local.rules` |
| Central logging | `logging host 10.10.60.11` | rsyslog forwarders → srv2 (UDP 514) (D29) | `configs/rsyslog/` |
| Admin SSH only from management | `line vty 0 4`, `access-class`, `transport input ssh` | sshd listening on the Management VLAN address; INPUT allows 22 only from 192.168.99.0/24 | `configs/ssh/`, `configs/iptables/rules.j2` |
| Network monitoring | SNMP + Cisco Prime / SolarWinds | Prometheus exporter + Grafana dashboard (D35) | `monitoring/` |
| Show commands | `show ip ospf neighbor`, `show vrrp brief`, `show crypto ipsec sa` | `vtysh -N r1 -c "show ip ospf neighbor"`, `... "show vrrp summary"`, `python3 topology/vpn.py status ...` | Mininet CLI |

## What Cisco has no direct equivalent for here

- **The explainable threat score** (D33): Cisco's tools give alerts; ours adds them into one score
  per host, needs two independent sources for a breach, and always says why.
- **Automatic containment at the access port** (D34): closest is Cisco ISE with rapid threat
  containment, which needs ISE, pxGrid and a Firepower integration. Ours is one REST call to the
  controller.
- **Self-measured recovery** (T25): the chaos test breaks the network on purpose, many times,
  and records how long each recovery took.
