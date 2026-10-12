const loc=d=>[d.city,d.state,d.country].filter(Boolean).join(', ');
run({
  title:'Venues', rows:DATA,
  row:d=>`<a class="dx-row dx-row--venue" href="#/venue/${d.id}">
    <span class="dx-body">
      <div class="dx-name">${esc(d.name)}</div>
      <div class="dx-sub">${esc(loc(d))}</div>
      ${d.text?`<div class="dx-text">${esc(d.text)}</div>`:''}
      ${d.top.length?`<div class="dx-with">${esc(d.top.join(', '))}${d.nartists>3?` and ${d.nartists-3} more`:''}</div>`:''}
    </span>
    <span class="dx-end">${photo(d,KIND)}<span class="dx-side"><div class="dx-n">${d.recs}</div><div class="dx-nl">${d.recs===1?'Recording':'Recordings'}</div>
      <div class="dx-span">${years(d.y0,d.y1)}</div></span></span></a>`,
  sorts:[{id:'name',label:'A-Z',cmp:byName},{id:'recs',label:'Recordings',cmp:(a,b)=>b.recs-a.recs||byName(a,b)},
         {id:'place',label:'Place',cmp:(a,b)=>(a.country||'').localeCompare(b.country||'')||(a.state||'').localeCompare(b.state||'')||(a.city||'').localeCompare(b.city||'')||byName(a,b)}],
  search:d=>[d.name,d.city,d.state,d.country,...d.top].join(' '),
  filter:{label:'All countries',get:d=>d.country}
});
