---
name: design-doc
description: Create and iterate versioned interactive HTML design docs that capture how a feature or area of the repository works. Writes vN.html to a configured docs directory (served by MkDocs or any static web server). The visual HTML lets you make architectural decisions at a big-picture level; the finalized doc becomes the basis for contracts, skills, and agent context. Includes a sticky-note annotation layer — mark up the doc in the browser, copy feedback back to Claude, iterate. Finalize button locks the design.
argument-hint: <feature or area name> [brief context or constraints]
---

# Design Doc Skill

Creates versioned HTML design docs that visually represent how a feature or area of the repository works — onboarding, input-vars, scan, whatever the focus is. The finalized doc is the source of truth that contracts, skills, and agent knowledge are built from.

Each invocation writes a new `vN.html` — never overwrites an existing version. The HTML includes a built-in annotation layer: place sticky notes in the browser, click "Copy Feedback" to get the notes as markdown, paste here → Claude creates vN+1. "Finalize" locks the doc and copies a summary.

## Invocation

```
/design-doc <topic>
/design-doc <topic> — <one-line context>
```

`$ARGUMENTS` = `$ARGUMENTS`

## Workflow

### Step 0 — Resolve base_url

design-doc is a separate skill from planish/plan-and-grill, but both ultimately point a browser at a local static file server, so they share ONE hostname setting instead of two. Resolve in this order, stop at the first match:

1. **Explicit override** — `.shared-llm/llm/claude/common/config/design-doc.json` or `.claude/design-doc.json` (checked in that order). If either sets `base_url`, use it verbatim (no trailing slash). Use this only when design-doc needs a *different* server than planish's — a different port, scheme, or docs root.
   ```json
   { "base_url": "http://your-machine-name:8088", "docs_dir": "ops/mkdocs/docs" }
   ```
   If this file also sets `docs_dir`, it wins in Step 1 too.
2. **Shared reference** — walk up from cwd for `.planish.yaml` (the same file `/planish` and `/do-plan-and-grill` read). If it has a `host:` field, `base_url = http://{host}:8089` (8089 is this kit's conventional static-docs-server port — see `.planish.yaml.example`).
3. **Default** — `base_url = http://localhost:8089`. Always usable: `python3 -m http.server 8089 --directory {docs_dir}` from Step 1 serves it.

`base_url` is never null — there is always a URL to report, even with zero config.

### Step 1 — Determine output directory

Use `docs_dir` from the config file that supplied `base_url` in Step 0 (case 1), if it set one. Otherwise check in this order:
1. `ops/mkdocs/docs/` exists → `ops/mkdocs/docs/diagrams/{slug}/`
2. `docs/diagrams/` exists → `docs/diagrams/{slug}/`
3. Otherwise → `/tmp/docs/diagrams/{slug}/`

`{slug}` = topic lowercased, spaces → hyphens, special chars stripped.

Create the directory if it does not exist. Full target path: `{docs_dir}/diagrams/{slug}/`.

### Step 2 — Version number

List `{dir}/v*.html`. If none exist → v1. Otherwise → find highest N, write v{N+1}.

```bash
ls {dir}/v*.html 2>/dev/null | sort -V | tail -1
```

### Step 3 — Read the project style

If `{docs_dir}/diagrams/architecture/v3.html` exists, read its `<style>` block and use it verbatim as the CSS foundation for the new doc. Otherwise use the default style block in Step 4.

### Step 4 — Write the HTML doc

Write `{dir}/v{N}.html` with:

1. The CSS (from v3.html if found, or the default block below)
2. Design content appropriate to the topic — use the visual vocabulary below
3. The annotation toolkit block (always — copy verbatim from the block at the end of this file)

**Default CSS** (use when v3.html is not present):

```html
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'JetBrains Mono',monospace;background:#0d1017;color:#c8ccd4;padding:40px;min-height:100vh;line-height:1.5;}
.page-header{margin-bottom:48px;padding-bottom:24px;border-bottom:1px solid #1e222a;}
.page-header h1{font-family:'IBM Plex Sans',sans-serif;font-size:22px;font-weight:600;color:#e6e9ef;letter-spacing:-0.3px;margin-bottom:6px;}
.page-header .subtitle{font-size:11px;color:#545862;letter-spacing:1.5px;text-transform:uppercase;}
.page-header .version{font-size:11px;color:#3e4450;margin-top:4px;}
.stage{margin-bottom:48px;}
.stage-header{display:flex;align-items:center;gap:12px;margin-bottom:20px;}
.stage-num{width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600;flex-shrink:0;}
.stage-title{font-family:'IBM Plex Sans',sans-serif;font-size:14px;font-weight:500;letter-spacing:0.5px;}
.stage-desc{font-size:10px;color:#545862;margin-left:40px;margin-top:-14px;margin-bottom:20px;letter-spacing:0.3px;line-height:1.6;}
.flow-row{display:flex;align-items:flex-start;gap:16px;overflow-x:auto;padding:4px 0 16px 0;}
.box{border:1px solid;border-radius:8px;padding:16px 18px;min-width:140px;flex-shrink:0;}
.box-title{font-size:11px;font-weight:600;letter-spacing:0.8px;text-transform:uppercase;margin-bottom:10px;}
.box-items{display:flex;flex-direction:column;gap:5px;}
.item{font-size:11px;color:#6b7280;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04);}
.item:last-child{border:none;}
.item.hi{color:#c8ccd4;}
.arrow{display:flex;flex-direction:column;align-items:center;justify-content:center;padding-top:36px;min-width:40px;flex-shrink:0;}
.arrow-shaft{width:36px;height:1.5px;background:#2e3440;position:relative;}
.arrow-shaft::after{content:'';position:absolute;right:-1px;top:-4px;border-left:7px solid #2e3440;border-top:4.5px solid transparent;border-bottom:4.5px solid transparent;}
.arrow-label{font-size:9px;color:#3e4450;text-align:center;margin-top:6px;letter-spacing:0.5px;max-width:48px;}
.chip-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;}
.chip{font-size:10px;padding:3px 8px;border-radius:4px;border:1px solid;}
.ref-section{margin-top:64px;padding-top:24px;border-top:1px solid #1e222a;}
.ref-title{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:#3e4450;margin-bottom:16px;}
.ref-grid{display:flex;gap:20px;flex-wrap:wrap;}
.ref-card{border:1px solid #2e3440;border-radius:6px;padding:14px 16px;min-width:180px;}
.ref-card-title{font-size:9px;letter-spacing:1.2px;text-transform:uppercase;color:#545862;margin-bottom:14px;}
.ref-item{display:flex;align-items:center;gap:8px;font-size:11px;color:#6b7280;margin-bottom:6px;}
.ref-dot{width:6px;height:6px;border-radius:2px;flex-shrink:0;}
/* Color palette — component roles */
.c-user{background:#111a14;border-color:#3a5a2a;} .c-user .box-title{color:#98c379;}
.c-saas{background:#18121e;border-color:#6a3a8a;} .c-saas .box-title{color:#c678dd;}
.c-aws{background:#1a1610;border-color:#8a5a1a;} .c-aws .box-title{color:#d19a66;}
.c-boundary{background:#161a1e;border-color:#3a4a5a;} .c-boundary .box-title{color:#6b7280;}
.c-db{background:#0f151d;border-color:#456a8a;} .c-db .box-title{color:#7ab4db;}
</style>
```

**Visual vocabulary:**

- `.box.c-user` — user-facing components (green)
- `.box.c-saas` — SaaS / platform layer (purple)
- `.box.c-aws` — AWS resources (orange)
- `.box.c-db` — databases (blue)
- `.box.c-boundary` — trust boundaries / interfaces (gray)
- `.flow-row` + `.arrow` — horizontal flow with labelled arrows
- `.chip-row` + `.chip` — color-coded legend chips
- `.ref-section` → `.ref-grid` → `.ref-card` — bottom reference appendix (list components + their sub-items with color dots)

**Structure template:**

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{Topic} — v{N}</title>
<!-- CSS here -->
</head>
<body>

<div class="page-header">
  <h1>{Topic}</h1>
  <div class="subtitle">{one-line description}</div>
  <div class="version">v{N} · {date}</div>
</div>

<!-- Stages: each is a logical group in the flow -->
<div class="stage">
  <div class="stage-header">
    <div class="stage-num" style="background:#111a14;color:#98c379;">1</div>
    <div class="stage-title" style="color:#98c379;">Stage Name</div>
  </div>
  <div class="stage-desc">One-line description of this stage.</div>
  <div class="flow-row">
    <div class="box c-user">
      <div class="box-title">Component</div>
      <div class="box-items">
        <div class="item hi">key item</div>
        <div class="item">secondary item</div>
      </div>
    </div>
    <div class="arrow"><div class="arrow-shaft"></div><div class="arrow-label">action</div></div>
    <!-- more boxes -->
  </div>
</div>

<!-- Reference appendix -->
<div class="ref-section">
  <div class="ref-title">Reference</div>
  <div class="ref-grid">
    <div class="ref-card">
      <div class="ref-card-title">Component Group</div>
      <div class="ref-item"><div class="ref-dot" style="background:#111a14;border:1px solid #3a5a2a;"></div>item one</div>
    </div>
  </div>
</div>

<!-- ANNOTATION TOOLKIT — always paste before </body> -->
</body>
</html>
```

### Step 5 — Report

After writing the file, report:

```
Created: {full path to vN.html}
URL:     {base_url}/diagrams/{slug}/vN.html

Use the sticky-note toolbar at the bottom to annotate.
When done, click "Copy Feedback" and paste it here to iterate → v{N+1}.
Click "Finalize" when the design is locked.
```

If `base_url` came from Step 0's default (case 3, no config found at all), add a one-liner:
```
Tip: this URL assumes a server at localhost:8089 serving /tmp/docs
(e.g. `python3 -m http.server 8089 --directory /tmp/docs`). Add
`host: <your-machine-name>` to .planish.yaml to point this at your real
docs server instead — same file /planish and /do-plan-and-grill use.
```

---

## Iteration — handling pasted feedback

When the user pastes output that starts with `## Feedback —` or `## FINALIZED —`:

1. Extract the filename from the `File:` line
2. Read the current vN.html
3. Understand the note annotations
4. Write v{N+1}.html addressing the feedback
5. Report the new path + URL

---

## Annotation Toolkit

**Always paste this block verbatim immediately before `</body>` in every design doc.**

```html
<!-- ═══════════════════════════════════════════════════════════
     DESIGN-DOC ANNOTATION LAYER  (do not edit this block)
     ═══════════════════════════════════════════════════════════ -->
<style>
  #desdoc-bar{position:fixed;bottom:0;left:0;right:0;background:#0d1017;border-top:1px solid #1e222a;
    padding:8px 16px;display:flex;gap:8px;align-items:center;z-index:9999;
    font-family:'JetBrains Mono',monospace;font-size:12px;color:#6b7280;}
  body{padding-bottom:48px!important;}
  .ddbtn{padding:4px 10px;border-radius:4px;border:1px solid #2e3440;background:#0d1017;
    color:#c8ccd4;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;white-space:nowrap;}
  .ddbtn:hover{background:#1e222a;}
  .ddbtn.copy{background:#1a2d4a;border-color:#456a8a;color:#7ab4db;}
  .ddbtn.fin{background:#0f1f14;border-color:#3a5a2a;color:#98c379;}
  #desdoc-cnt{background:#2e3440;color:#c8ccd4;padding:1px 6px;border-radius:9999px;font-size:10px;margin-left:2px;}
  #desdoc-badge{display:none;position:fixed;top:0;left:0;right:0;background:#0f1f14;
    border-bottom:1px solid #3a5a2a;color:#98c379;text-align:center;
    padding:5px 16px;font-family:'JetBrains Mono',monospace;font-size:11px;z-index:10000;}
</style>
<div id="desdoc-badge">✓ FINALIZED — summary copied to clipboard</div>
<div id="desdoc-bar">
  <span style="color:#3e4450;margin-right:4px;">design-doc</span>
  <button class="ddbtn" onclick="ddAdd()">+ Note</button>
  <button class="ddbtn" onclick="ddToggle()">Notes <span id="desdoc-cnt">0</span></button>
  <button class="ddbtn copy" id="ddcopybtn" onclick="ddCopy()">Copy Feedback</button>
  <button class="ddbtn fin" onclick="ddFinalize()">Finalize ✓</button>
</div>
<script>
(function(){
  const KEY='desdoc__'+btoa(location.pathname).replace(/[+/=]/g,'');
  let notes=[],ctr=0,vis=true;
  function save(){
    localStorage.setItem(KEY,JSON.stringify(
      notes.map(n=>({id:n.id,x:parseFloat(n.el.style.left),y:parseFloat(n.el.style.top),
        text:n.el.querySelector('textarea').value}))));
    document.getElementById('desdoc-cnt').textContent=notes.length;
  }
  function mk(x,y,id,text){
    id=id||String(++ctr);
    const el=document.createElement('div');
    el.style.cssText='position:absolute;left:'+x+'px;top:'+y+'px;z-index:9000;min-width:180px;max-width:260px;'
      +'background:#1c1a10;border:1px solid #8a6a1a;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.4);';
    el.innerHTML='<div style="background:#8a6a1a;padding:3px 8px;cursor:move;display:flex;justify-content:space-between;'
      +'align-items:center;border-radius:4px 4px 0 0;font:11px/20px \'JetBrains Mono\',monospace;color:#1a1208;user-select:none;">'
      +'<span>Note '+id+'</span>'
      +'<span onclick="ddDel(\''+id+'\')" style="cursor:pointer;font-weight:700;padding:0 3px;color:#3a2808;">✕</span></div>'
      +'<textarea placeholder="Add note…" style="width:100%;border:none;background:transparent;padding:6px 8px;'
      +'font:12px/1.5 \'JetBrains Mono\',monospace;color:#c8ccd4;resize:vertical;min-height:56px;box-sizing:border-box;outline:none;">'+(text||'')+'</textarea>';
    document.body.appendChild(el);
    el.querySelector('textarea').addEventListener('input',save);
    const h=el.firstElementChild;
    h.addEventListener('mousedown',function(e){
      if(e.target.onclick)return;
      const ox=e.clientX-el.getBoundingClientRect().left,oy=e.clientY-el.getBoundingClientRect().top;
      const mv=function(e2){el.style.left=(e2.clientX-ox+scrollX)+'px';el.style.top=(e2.clientY-oy+scrollY)+'px';};
      const up=function(){save();removeEventListener('mousemove',mv);removeEventListener('mouseup',up);};
      addEventListener('mousemove',mv);addEventListener('mouseup',up);e.preventDefault();
    });
    notes.push({id,el});save();
  }
  window.ddDel=function(id){const i=notes.findIndex(n=>n.id===id);if(i<0)return;notes[i].el.remove();notes.splice(i,1);save();};
  window.ddAdd=function(){
    document.body.style.cursor='crosshair';
    const h=function(e){if(e.target.closest('#desdoc-bar'))return;
      document.body.style.cursor='';removeEventListener('click',h);mk(e.pageX-90,e.pageY-20,null,'');};
    addEventListener('click',h);
  };
  window.ddToggle=function(){vis=!vis;notes.forEach(n=>n.el.style.display=vis?'':'none');};
  window.ddCopy=function(){
    const items=notes.map(n=>'- [Note '+n.id+'] '+(n.el.querySelector('textarea').value||'(empty)')).join('\n');
    const out='## Feedback — '+document.title+'\nFile: '+location.pathname+'\n\n'+(items||'(no notes)');
    navigator.clipboard.writeText(out).then(function(){
      const b=document.getElementById('ddcopybtn');const t=b.textContent;b.textContent='Copied ✓';setTimeout(function(){b.textContent=t;},2000);
    });
  };
  window.ddFinalize=function(){
    if(!confirm('Mark this design doc as finalized?'))return;
    const items=notes.map(n=>'- [Note '+n.id+'] '+(n.el.querySelector('textarea').value||'(empty)')).join('\n');
    const out='## FINALIZED — '+document.title+'\nFile: '+location.pathname+'\n\n'+(items?'Final notes:\n'+items:'(no notes — approved as-is)');
    navigator.clipboard.writeText(out);
    localStorage.setItem(KEY+'__final','1');
    document.getElementById('desdoc-badge').style.display='block';
    document.body.style.paddingTop='32px';
  };
  var saved=JSON.parse(localStorage.getItem(KEY)||'[]');
  if(saved.length)ctr=saved.reduce(function(m,n){return Math.max(m,parseInt(n.id)||0);},0);
  saved.forEach(function(n){mk(n.x,n.y,n.id,n.text);});
  if(localStorage.getItem(KEY+'__final')==='1'){
    document.getElementById('desdoc-badge').style.display='block';
    document.body.style.paddingTop='32px';
  }
})();
</script>
<!-- ═══════════════════════════════════════════════════════════ -->
```
