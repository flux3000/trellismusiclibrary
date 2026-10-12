/* Three alternatives over the same rows. Genres are few (13) and have no
   photographs or descriptions, so no search or filter in any of them. */
const G=DATA.slice().sort((a,b)=>b.recs-a.recs);
const maxR=Math.max(...G.map(g=>g.recs));
const DEC=[1950,1960,1970,1980,1990,2000,2010,2020];
const maxD=Math.max(...G.flatMap(g=>DEC.map(d=>g.dec[d]||0)));
const fg=g=>g.color?`--dx-fg:${g.color}`:'';
const num=(n,w)=>`<div class="dx-n">${n}</div><div class="dx-nl">${w}</div>`;
const A=`<div class="dx-opt">A. Composition strip and ledger</div>
 <div class="gx-strip">${G.filter(g=>g.recs).map(g=>`<i style="${fg(g)};flex:${g.recs}" title="${esc(g.name)}"></i>`).join('')}</div>
 <div class="dx-list">${G.map(g=>`<a class="gx-row" href="#/genre/${g.id}" style="${fg(g)}"><span class="dx-sw"></span>
   <span class="dx-name">${esc(g.name)}</span><span class="dx-side">${num(g.nartists,'Artists')}</span><span class="dx-side">${num(g.recs,'Recordings')}</span></a>`).join('')}</div>`;
const B=`<div class="dx-opt">B. Colour key and roster</div>
 <div class="dx-list">${G.map(g=>`<a class="gb-row" href="#/genre/${g.id}" style="${fg(g)}">
   <span class="gb-key" style="color:${inkOn(g.color)}">${g.recs}</span>
   <span class="dx-body"><div class="dx-name">${esc(g.name)}</div><div class="dx-sub">${plural(g.nartists,'artist')}</div>
   <div class="gb-faces">${g.faces.map(f=>art(f,'artists',g.color)).join('')}</div></span></a>`).join('')}</div>`;
const C=`<div class="dx-opt">C. Decades</div>
 <div class="gc-head"><span></span><div class="gc-dec" style="--cols:${DEC.length}">${DEC.map(d=>`<span>${String(d).slice(2)}s</span>`).join('')}</div><span></span></div>
 <div class="dx-list">${G.map(g=>`<a class="gc-row" href="#/genre/${g.id}" style="${fg(g)}">
   <span class="dx-name"><span class="dx-sw"></span>${esc(g.name)}</span>
   <span class="gc-dec" style="--cols:${DEC.length}">${DEC.map(d=>{const n=g.dec[d]||0;return n?`<i style="--p:${Math.round(18+82*Math.sqrt(n/maxD))}%" title="${n}"></i>`:'<i></i>'}).join('')}</span>
   <span class="dx-side"><div class="dx-n">${g.recs}</div></span></a>`).join('')}</div>`;
document.getElementById('page').innerHTML=`<h1 class="dx-title">Genres</h1>${A}${B}${C}`;
