"""Adapt the installed road extract to existing direction-aware stop matchers."""


def local_elements(point, radius_m):
    try:
        from openpilot.selfdrive.nap.offline_roads import OfflineRoadStore
        result = OfflineRoadStore().lookup(point[1], point[0], radius_m)
        if result is None:
            return None
        data, _ = result
        elements = data['elements']
        nodes = {e['id']: e for e in elements if e.get('type') == 'node'}
        for way in elements:
            if way.get('type') == 'way':
                way['geometry'] = [{'lat': nodes[n]['lat'], 'lon': nodes[n]['lon']}
                                   for n in way['nodes']]
        return elements
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return None
