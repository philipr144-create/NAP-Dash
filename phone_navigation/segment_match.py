"""Project onto route segments before using heading to resolve nearby choices."""
import math


def nearest_segment(points, x, y, heading, start, end, xy):
    candidates = []
    for index in range(start, end):
        a, aa = points[index]
        b, ba = points[index + 1]
        ax, ay = xy(a)
        bx, by = xy(b)
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        if length <= 1e-8 or ba <= aa:
            continue
        fraction = max(0., min(1., ((x-ax)*dx + (y-ay)*dy) / length))
        error = math.hypot(x-ax-fraction*dx, y-ay-fraction*dy)
        course = math.degrees(math.atan2(dx, dy))
        heading_error = None if heading is None else abs((course-heading+180) % 360-180)
        candidates.append((error, aa + fraction*(ba-aa), heading_error))
    if not candidates:
        return None, False
    nearest = min(candidates, key=lambda item: item[0])
    nearby = [item for item in candidates if item[0] <= nearest[0]+8]
    # A plausible heading may disambiguate nearby geometry, but must never
    # make a distant segment win. Retain heading error for downstream gates.
    aligned = [item for item in nearby if item[2] is not None and item[2] <= 35]
    eligible = aligned or nearby
    best = min(eligible, key=lambda item: item[0])
    ambiguous = any(abs(item[1]-best[1]) > 60 for item in eligible)
    return best, ambiguous
