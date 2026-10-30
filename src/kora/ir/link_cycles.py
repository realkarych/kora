from collections.abc import Iterator


def cyclic_vertices(edges: dict[str, set[str]]) -> set[str]:
    reverse: dict[str, list[str]] = {name: [] for name in edges}
    for source, targets in edges.items():
        for target in targets:
            reverse[target].append(source)
    seen: set[str] = set()
    finished: list[str] = []
    for root in edges:
        if root in seen:
            continue
        seen.add(root)
        pending: list[tuple[str, Iterator[str]]] = [(root, iter(edges[root]))]
        while pending:
            name, targets = pending[-1]
            target = next(targets, None)
            if target is None:
                finished.append(name)
                pending.pop()
            elif target not in seen:
                seen.add(target)
                pending.append((target, iter(edges[target])))
    seen.clear()
    cyclic: set[str] = set()
    for root in reversed(finished):
        if root in seen:
            continue
        component: list[str] = []
        names = [root]
        seen.add(root)
        while names:
            name = names.pop()
            component.append(name)
            for predecessor in reverse[name]:
                if predecessor not in seen:
                    seen.add(predecessor)
                    names.append(predecessor)
        if len(component) > 1 or root in edges[root]:
            cyclic.update(component)
    return cyclic
