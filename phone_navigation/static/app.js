let lastStatusAt=null;
const $=id=>document.getElementById(id);
let key='',selected=null,connected=false,busy=false,polling=false,routeActive=false,navSwitchBusy=false;
try{key=localStorage.getItem('nap-phone-key')||'';
  const linked=new URLSearchParams(location.hash.slice(1)).get('key');
  if(linked){key=linked;localStorage.setItem('nap-phone-key',key);history.replaceState(null,'',location.pathname);}
}catch{ }
function connection(ok){
  connected=ok;$('connection').textContent=ok?'Connected':'Not connected';$('pairing').hidden=ok;
  $('destination').disabled=!ok;$('search').disabled=!ok||busy;$('nav-influence').disabled=!ok||navSwitchBusy;
}
async function api(action,data={}){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
  try{
    const response=await fetch('/phone-nav/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-NAP-Phone-Key':key},body:JSON.stringify(data),signal:controller.signal});
    let result;try{result=await response.json();}catch{throw Error('The comma returned an unexpected response. Reload this page.');}
    if(response.status===401){connection(false);$('search-status').textContent='Connect this browser using the setup code above.';}
    if(!response.ok)throw Error(result.error||'Request failed');return result;
  }catch(error){if(error.name==='AbortError')throw Error('Request timed out. Check the comma cellular or hotspot connection.');throw error;
  }finally{clearTimeout(timer);}
}
$('pair-form').onsubmit=async event=>{
  event.preventDefault();$('connect').disabled=true;$('pair-status').textContent='Connecting…';
  try{const result=await api('pair',{code:$('pair-code').value.trim()});key=result.key;
    try{localStorage.setItem('nap-phone-key',key);}catch{ }
    $('pair-code').value='';$('pair-status').textContent='';await refresh();
    if(connected){$('search-status').textContent='Enter an address or place, including the city.';await refreshFavorites();}
  }catch(error){$('pair-status').textContent=error.message;}finally{$('connect').disabled=false;}
};
$('search-form').onsubmit=async event=>{
  event.preventDefault();if(!connected||busy)return;
  busy=true;$('search').disabled=true;$('search-status').textContent='Searching…';$('results').replaceChildren();
  selected=null;$('start').hidden=true;
  try{const result=await api('search',{query:$('destination').value});
    $('search-status').textContent=result.places.length?'Choose the correct result below.':'No matches found. Try the street address and city.';
    for(const place of result.places){const button=document.createElement('button');button.type='button';button.textContent=place.label;
      button.onclick=()=>{selected=place;$('instruction').textContent=place.label;$('results').replaceChildren();$('start').hidden=false;
        $('start').textContent=routeActive?'Replace route on comma':'Start route on comma';$('search-status').textContent='Destination selected. Tap Start to send it to the comma.';};
      $('results').append(button);}
  }catch(error){$('search-status').textContent=error.message;}finally{busy=false;$('search').disabled=!connected;}
};
function render(result){
  lastStatusAt=performance.now();
  connection(true);routeActive=result.active;
  $('gps').textContent=result.gps_ready?(result.accuracy_m?`Comma GPS ready · estimated accuracy ${Math.round(result.accuracy_m)} m`:'Comma GPS ready · accuracy estimate unavailable'):'Waiting for comma GPS. Turn the car on; GPS needs a clear view of the sky.';
  $('status').textContent=result.message;$('stop').hidden=!result.active;
  if(!navSwitchBusy){$('nav-influence').checked=result.enabled===true;$('nav-influence').indeterminate=false;}
  const bridge=result.bridge||{};
  $('influence-status').textContent=`${bridge.state==='ready'?'ON · guidance available':bridge.state==='paused'?'ON · guidance PAUSED':bridge.state==='off'?'OFF · directions only':'Bridge state unavailable'}: ${bridge.reason||'Restart to load bridge diagnostics.'}`;
  const nav=result.navigation||{},m=nav.maneuver;
  const decision=result.model_decision||{};
  const context=nav.route_context;
  if(context){
    const next=(context.upcoming||[]).map(m=>`${m.type} ${m.modifier} in ${Math.max(0,Math.round(m.distance_m))} m`).join(' → ');
    $('status').textContent+=` Local route · GPS age ${context.gps_age_s}s · distance from route ${context.match_error_m} m. Upcoming: ${next}`;
  }

  $('nav-maneuver').textContent=m?`${m.type} ${m.modifier||''} · ${Math.max(0,Math.round(m.distance_m))} m · ${result.gps_ready?'GPS fresh':'GPS unavailable'}`:'No active maneuver';
  $('nav-desire').textContent=decision.model_evaluated===true?`Selected model desire: ${decision.final_desire} · source: ${decision.source}`:'Selected model desire unavailable · model offroad, stale, or inference skipped';
  $('nav-reason').textContent=`Navigation request: ${decision.desire||'none'} · ${decision.reason||'Diagnostics unavailable; restart may be needed'}`;
  const stops=result.mapped_stops||{};
  $('mapped-stop-status').textContent=!stops.enabled?'Off':stops.state==='loading'?'Loading mapped stops ahead…':stops.state==='unavailable'?`Map lookup unavailable; no stop slowdown. ${stops.error||'Check internet connection.'}`:stops.state==='partial'?`Using cached signs; map refresh failed. ${stops.error||''}`:stops.state==='ready'?(stops.next_distance_m!=null?`Mapped stop sign ${Math.max(0,Math.round(stops.next_distance_m))} m ahead · ${!result.enabled?'speed hint off: navigation influence is off':!result.accuracy_m?'speed hint waiting for GPS accuracy':'speed hint available with active speed control'}`:`Map loaded · ${stops.loaded_ahead||0} matched stop signs ahead · ${Math.round((stops.coverage_remaining_m||0)/1000)} km cached`):'Waiting for route and GPS';
  const road=result.road_stops||{};
  $('road-stop-status').textContent=!road.enabled?'Off':road.state==='ready'?(road.next_distance_m!=null?`${road.road||'Current road'} · mapped stop ${Math.round(road.next_distance_m)} m ahead · rolling-speed hint only`:`Matched ${road.road||'current road'} · no direction-matched stop ahead`):road.state==='unavailable'?`Map lookup unavailable: ${road.error||'check connection'}`:`${road.state||'Waiting for road match'}${road.last_response_bytes?` · last lookup ${(road.last_response_bytes/1024).toFixed(0)} KB`:''}`;
  if(!selected){
    $('instruction').textContent=m?.primary_text||result.destination?.label||'Choose your destination';
    $('distance').textContent=nav.route_state==='arrived'?'You have arrived':m?`${Math.max(0,Math.round(m.distance_m*3.28084))} ft`:result.active?'Waiting for route guidance':'Ready when you are';
    $('arrow').textContent=nav.route_state==='arrived'?'✓':(m?.modifier||'').includes('left')?'←':(m?.modifier||'').includes('right')?'→':'↑';
    $('trip').textContent=m?`${Math.max(1,Math.round(nav.time_remaining_s/60))} min · ${(nav.distance_remaining_m/1609.344).toFixed(1)} mi remaining · no live traffic`:'';
  }
}
async function refresh(){
  if(!key||polling||document.hidden)return;polling=true;
  try{render(await api('status'));if($('search-status').textContent==='Connect this browser to search for destinations.')$('search-status').textContent='Enter an address or place, including the city.';
  }catch(error){lastStatusAt=null;connection(false);$('connection').textContent='Connection unavailable';$('status').textContent=error.message;$('nav-influence').disabled=true;$('nav-influence').indeterminate=true;$('influence-status').textContent='Connection lost: navigation switch state is unknown. Guidance freshness checks remain active.';}
  finally{polling=false;}
}
$('start').onclick=async()=>{
  if(!selected)return;$('start').disabled=true;
  try{const result=await api('start',{destination:selected});selected=null;$('start').hidden=true;render(result);$('search-status').textContent=result.enabled?'Destination sent to the comma.':'Route started. Navigation influence is OFF; switch it on for automatic signals and mapped-stop speed hints.';}
  catch(error){$('search-status').textContent=error.message;}finally{$('start').disabled=false;}
};
$('stop').onclick=async()=>{
  $('stop').disabled=true;try{selected=null;render(await api('stop'));$('start').hidden=true;}
  catch(error){$('status').textContent=error.message;}finally{$('stop').disabled=false;}
};
$('nav-influence').onchange=async()=>{
  navSwitchBusy=true;$('nav-influence').disabled=true;
  try{const result=await api('influence',{enabled:$('nav-influence').checked});navSwitchBusy=false;render(result);}
  catch(error){navSwitchBusy=false;$('status').textContent=error.message;$('nav-influence').indeterminate=true;$('influence-status').textContent='Switch update failed; waiting for confirmed backend state.';await refresh();}
};
connection(false);if(key){$('connection').textContent='Connecting…';refresh().then(refreshFavorites);}
setInterval(refresh,1000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});

async function refreshTurnSettings(){
  try{const t=await api('turn-settings');
    $('manual-blinker-turns').checked=t.manual_blinker_turns;$('manual-blinker-turns').disabled=false;
    $('corner-assist').checked=t.corner_assist;$('blinker-braking').checked=t.blinker_braking;
    $('mapped-stop-slowdown').checked=t.mapped_stop_slowdown===true;
    $('road-stop-slowdown').checked=t.road_stop_slowdown===true;
    $('normal-model-stops').checked=t.normal_model_stops===true;
    $('normal-model-stops').disabled=false;
    $('corner-assist').disabled=$('blinker-braking').disabled=false;
    $('mapped-stop-slowdown').disabled=false;
    $('road-stop-slowdown').disabled=false;
    $('turn-setting-status').textContent='Settings saved. The updated planner must be loaded for these to take effect.';
  }catch(e){$('normal-model-stops').disabled=$('manual-blinker-turns').disabled=$('corner-assist').disabled=$('blinker-braking').disabled=$('mapped-stop-slowdown').disabled=$('road-stop-slowdown').disabled=true;$('turn-setting-status').textContent=e.message;}
}
for(const [id,name] of [['corner-assist','corner_assist'],['blinker-braking','blinker_braking'],['manual-blinker-turns','manual_blinker_turns'],['mapped-stop-slowdown','mapped_stop_slowdown'],['road-stop-slowdown','road_stop_slowdown'],['normal-model-stops','normal_model_stops']]){
  $(id).onchange=async()=>{const enabled=$(id).checked;$(id).disabled=true;
    try{await api('turn-setting',{name,enabled});await refreshTurnSettings();}
    catch(e){$(id).checked=!enabled;$(id).disabled=!connected;$('turn-setting-status').textContent=e.message;}
  };
}
setInterval(()=>{if(connected&&!document.hidden)refreshTurnSettings();},5000);

// Presets are stored on the comma so the device screen can use them.
let favorites=[];
function drawFavorites(){
 $('saved-places').replaceChildren();
 for(const place of favorites){
  const row=document.createElement('div'),choose=document.createElement('button'),remove=document.createElement('button');
  choose.type=remove.type='button';choose.textContent=place.name;remove.textContent='×';remove.setAttribute('aria-label','Remove '+place.name);
  choose.onclick=()=>{selected={label:place.label,location:[...place.location]};$('instruction').textContent=place.label;$('start').hidden=false;$('saved-name').value=place.name;$('saved-status').textContent='Selected. Tap Start to send this destination.';};
  remove.onclick=async()=>{try{const result=await api('preset-remove',{name:place.name});favorites=result.presets;drawFavorites();$('saved-status').textContent='Removed from comma.';}catch(e){$('saved-status').textContent=e.message;}};
  row.append(choose,remove);$('saved-places').append(row);
 }
}
async function refreshFavorites(){
 if(!connected)return;
 try{favorites=(await api('presets')).presets;drawFavorites();}catch(e){$('saved-status').textContent=e.message;}
}
$('save-place').onclick=async()=>{
 if(!selected)return;
 const name=$('saved-name').value.trim();if(!name){$('saved-status').textContent='Enter a short name first.';return;}
 $('save-place').disabled=true;
 try{const result=await api('preset-save',{name,destination:selected});favorites=result.presets;drawFavorites();$('saved-status').textContent='Saved on comma. Available from Routes on its home screen.';}
 catch(e){$('saved-status').textContent=e.message;}
 finally{$('save-place').disabled=false;}
};
setInterval(()=>{
 $('save-place').disabled=!selected;
 const age=lastStatusAt===null?Infinity:(performance.now()-lastStatusAt)/1000;
 const stale=age>3;
 document.querySelector('.intent-panel').classList.toggle('stale',stale);
 $('intent-age').textContent=Number.isFinite(age)?`${stale?'STALE · ':''}Received ${Math.floor(age)}s ago`:'Waiting for live update…';
 if(stale){$('nav-desire').textContent='LIVE INTENT UNAVAILABLE';$('nav-reason').textContent='Connection or status is stale. Last maneuver shown above may be outdated.';}
},500);
window.addEventListener('online',()=>refresh());
window.addEventListener('pageshow',()=>refresh());
