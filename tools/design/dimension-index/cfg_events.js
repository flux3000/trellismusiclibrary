run({
  title:'Events', rows:DATA,
  row:d=>`<a class="dx-row dx-row--event" href="#/event/${d.id}">
    <span class="dx-body">
      ${d.date?`<div class="dx-date">${esc(d.date)}</div>`:''}
      <div class="dx-name">${esc(d.name)}</div>
      <div class="dx-sub">${esc([d.venue,d.place].filter(Boolean).join(', '))}</div>
      ${d.text?`<div class="dx-text">${esc(d.text)}</div>`:''}
      ${d.lineup.length?`<div class="dx-with">${esc(d.lineup.join(', '))}${d.nartists>4?` and ${d.nartists-4} more`:''}</div>`:''}
    </span>
    <span class="dx-end">${photo(d,KIND)}<span class="dx-side"><div class="dx-n">${d.recs}</div><div class="dx-nl">${d.recs===1?'Recording':'Recordings'}</div></span></span></a>`,
  sorts:[{id:'date',label:'Date',cmp:(a,b)=>(a.key||9e9)-(b.key||9e9)||byName(a,b)},{id:'name',label:'A-Z',cmp:byName},
         {id:'recs',label:'Recordings',cmp:(a,b)=>b.recs-a.recs||byName(a,b)}],
  search:d=>[d.name,d.venue,d.place,...d.lineup].join(' '),
  group:{sort:'date',key:d=>d.year||0,label:d=>d.year||''}
});
