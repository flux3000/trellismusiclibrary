run({
  title:'Artists', rows:DATA,
  row:d=>`<a class="dx-row dx-row--artist" href="#/artist/${d.id}" style="--dx-fg:${esc(d.color||'')}">
    ${art(d,KIND,d.color)}
    <span class="dx-body">
      <div class="dx-name">${esc(d.name)}</div>
      <div class="dx-sub">${d.genre?`<span class="dx-sw"></span>${esc(d.genre)}`:''}${d.genre&&d.area?' · ':''}${esc(d.area||'')}</div>
      ${d.text?`<div class="dx-text">${esc(d.text)}</div>`:''}
      ${d.members.length?`<div class="dx-with"><b>${d.nmembers>d.members.length?'':''}</b>${esc(d.members.slice(0,5).join(', '))}${d.nmembers>5?` and ${d.nmembers-5} more`:''}</div>`:''}
    </span>
    <span class="dx-side"><div class="dx-n">${d.recs}</div><div class="dx-nl">${d.recs===1?'Recording':'Recordings'}</div>
      <div class="dx-span">${years(d.y0,d.y1)}</div></span></a>`,
  sorts:[{id:'name',label:'A-Z',cmp:byName},{id:'recs',label:'Recordings',cmp:(a,b)=>b.recs-a.recs||byName(a,b)},
         {id:'old',label:'Earliest',cmp:(a,b)=>(a.y0||9999)-(b.y0||9999)||byName(a,b)}],
  search:d=>[d.name,d.genre,d.area,...d.members].join(' '),
  filter:{label:'All genres',get:d=>d.genre}
});
