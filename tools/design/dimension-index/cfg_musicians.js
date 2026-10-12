run({
  title:'Musicians', rows:DATA,
  row:d=>`<a class="dx-row dx-row--musician" href="#/musician/${d.id}">
    ${art(d,KIND,null)}
    <span class="dx-body">
      <div class="dx-name">${esc(d.name)}</div>
      <div class="dx-sub">${esc(d.inst.join(', '))}</div>
      ${d.text?`<div class="dx-text">${esc(d.text)}</div>`:''}
      ${d.artists.length?`<div class="dx-with">${esc(d.artists.join(', '))}${d.nartists>3?` and ${d.nartists-3} more`:''}</div>`:''}
    </span>
    <span class="dx-side"><div class="dx-n">${d.recs||''}</div>${d.recs?`<div class="dx-nl">${d.recs===1?'Recording':'Recordings'}</div>`:''}</span></a>`,
  sorts:[{id:'name',label:'A-Z',cmp:byName},{id:'recs',label:'Recordings',cmp:(a,b)=>b.recs-a.recs||byName(a,b)}],
  search:d=>[d.name,...d.inst,...d.artists].join(' '),
  group:{sort:'name',key:d=>((d.sort||d.name)[0]||'#').toUpperCase(),label:d=>((d.sort||d.name)[0]||'#').toUpperCase()}
});
