const R = 6371000, rad = Math.PI / 180;
export function distance(a, b) {
  const dlat = (b[1]-a[1])*rad, dlon = (b[0]-a[0])*rad;
  const h = Math.sin(dlat/2)**2 + Math.cos(a[1]*rad)*Math.cos(b[1]*rad)*Math.sin(dlon/2)**2;
  return 2*R*Math.asin(Math.sqrt(Math.min(1,h)));
}
export function prepareRoute(route) {
  const points = [], steps = []; let length = 0;
  for (const leg of route.legs) for (const step of leg.steps) {
    steps.push({...step, at: length});
    for (const p of step.geometry.coordinates) {
      if (points.length) length += distance(points.at(-1).p,p);
      points.push({p,at:length});
    }
  }
  if (points.length < 2 || length < 1) throw Error('No usable route geometry');
  return {points,steps,length,duration:route.duration};
}
export function match(route, location, progress = 0, ahead = 160) {
  let best = null;
  for (let i=1;i<route.points.length;i++) {
    const a = route.points[i-1], b = route.points[i];
    if (b.at < progress-25 || a.at > progress+ahead) continue;
    const scale = Math.cos(location[1]*rad), x=(b.p[0]-a.p[0])*scale, y=b.p[1]-a.p[1];
    const u=(location[0]-a.p[0])*scale, v=location[1]-a.p[1];
    const t=Math.max(0,Math.min(1,(u*x+v*y)/(x*x+y*y||1)));
    const projected=[a.p[0]+(b.p[0]-a.p[0])*t,a.p[1]+(b.p[1]-a.p[1])*t];
    const error=distance(location,projected), at=a.at+(b.at-a.at)*t;
    if (!best || error < best.error) best={error,at};
  }
  return best;
}
export function guidance(route, progress) {
  const next=route.steps.find(s=>s.at>progress+6) || route.steps.at(-1);
  const remaining=Math.max(0,route.length-progress);
  return {next,remaining,distance:Math.max(0,next.at-progress),seconds:route.duration*remaining/route.length};
}
export function instruction(step) {
  const m=step.maneuver, road=step.name ? ` onto ${step.name}` : '';
  if (m.type==='arrive') return 'Arrive at your destination';
  if (m.type==='roundabout' || m.type==='rotary') return `Take exit ${m.exit||''} at the roundabout${road}`;
  if (m.type==='depart') return `Head ${m.modifier||'along the road'}${road}`;
  return `${m.type==='fork'?'Keep':m.type==='merge'?'Merge':'Continue'} ${m.modifier||'straight'}${road}`;
}
