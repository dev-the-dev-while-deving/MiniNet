"""paths.py: which way each switch sends traffic for a destination.

For a destination switch D, we compute ONE shortest-path tree rooted at D and give every
switch the next hop along it. Because all switches use the same tree, their choices always
agree and can never form a loop (two switches pointing at each other).

Link weights: links touching the core c2 cost 2, all others 1, so traffic normally crosses
c1 (the same core RSTP used as root, decisions D16) and c2 is the standby core. Without
this, equal-cost ties would pick c1 or c2 depending on discovery order, and the demo's
"link c1 s1 down" might not touch the path in use.

backup_next_hop() is for proactive fast-failover (T11): the next hop to use when the
primary link of a switch fails, found by removing that link and recomputing.
"""
import networkx as nx


def weight(g, core2):
    def w(a, b, _attrs):
        return 2 if core2 in (a, b) else 1
    return w


def tree_to(g, dst, core2):
    """{switch: next-hop switch} for every switch that can reach dst (dst itself excluded)."""
    if dst not in g:
        return {}
    paths = nx.single_source_dijkstra_path(g, dst, weight=weight(g, core2))
    return {sw: path[-2] for sw, path in paths.items() if sw != dst}


def path_to(g, src, dst, core2):
    """The switch path src -> dst along dst's tree (for logs and the REST API)."""
    tree = tree_to(g, dst, core2)
    if src == dst:
        return [src]
    if src not in tree:
        return []
    path = [src]
    while path[-1] != dst:
        path.append(tree[path[-1]])
    return path


def backup_next_hop(g, sw, primary, dst, core2):
    """The next hop sw should use towards dst if its link to `primary` dies, or None."""
    h = g.copy()
    h.remove_edge(sw, primary)
    try:
        p = nx.dijkstra_path(h, sw, dst, weight=weight(h, core2))
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None
    return p[1] if len(p) > 1 else None
