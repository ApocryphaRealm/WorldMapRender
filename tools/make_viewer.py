"""Build one map viewer page: a big plain label, wheel zoom at the cursor, drag to pan.

    make_viewer.py <mod data folder> [out folder]     the DDS pictures the game loads, full size: what zooming shows is what the game has
    make_viewer.py <folder of NN-Name-day.jpg ...> [out.html]     small pictures, embedded in one file

From a data folder it writes <out>/maps-viewer.html and <out>/full/*.jpg beside it, so the page needs that folder kept with it.
"""
import base64, glob, json, os, re, sys

from PIL import Image

Image.MAX_IMAGE_PIXELS = None
HERE = os.path.dirname(os.path.abspath(__file__))
folder = sys.argv[1]
tdir = os.path.join(folder, "textures", "terrain", "worldmaprender")
maps = {}
if os.path.isdir(tdir):
    root = sys.argv[2] if len(sys.argv) > 2 else folder
    out = os.path.join(root, "maps-viewer.html")
    os.makedirs(os.path.join(root, "full"), exist_ok=True)
    cfg = json.load(open(os.path.join(HERE, "worlds.json"), encoding="utf-8"))
    n = 0
    for ed, c in cfg.items():
        if ed.startswith("_"):
            continue
        n += 1
        low = ed.lower()
        single = c.get("single")
        for tag, f in (((single, low + ".dds"),) if single else (("day", low + ".dds"), ("night", low + "_n.dds"))):
            src = os.path.join(tdir, f)
            if not os.path.isfile(src):
                continue
            name = "%02d-%s-%s.jpg" % (n, c["name"].replace(" ", "-"), tag)
            dest = os.path.join(root, "full", name)
            if not os.path.isfile(dest) or os.path.getmtime(dest) < os.path.getmtime(src):
                im = Image.open(src)
                im.load()
                im.convert("RGB").save(dest, quality=93, subsampling=0)
                print(name, im.size, flush=True)
            entry = maps.setdefault(n, {"num": n, "name": c["name"], "pics": {}})
            entry["pics"][tag] = "full/" + name
else:
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(folder, "maps-viewer.html")
    for p in sorted(glob.glob(os.path.join(folder, "*.jpg"))):
        g = re.match(r"(\d+)-(.+)-(day|night)\.jpg$", os.path.basename(p))
        if not g:
            continue
        name = g.group(2).replace("-", " ").replace("Cyrodiil", "Bruma")
        entry = maps.setdefault(g.group(1), {"num": int(g.group(1)), "name": name, "pics": {}})
        entry["pics"][g.group(3)] = "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()
data = json.dumps([maps[k] for k in sorted(maps)])
page = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Map Viewer</title>
<style>
:root{--bg:#0b0b0d;--panel:#17171b;--line:#3a3a42;--text:#f4f4f6;--dim:#a4a4ae}
html,body{height:100%;margin:0;background:var(--bg);color:var(--text);font:16px system-ui,sans-serif;overflow:hidden}
#top{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line)}
button{background:#25252b;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font:inherit;cursor:pointer}
button.on{background:var(--text);color:#000;border-color:var(--text)}
#label{padding:6px 16px;font-size:44px;font-weight:700;line-height:1.15;background:var(--panel);border-bottom:1px solid var(--line)}
#label small{font-size:22px;font-weight:400;color:var(--dim);margin-left:14px}
#view{position:absolute;left:0;right:0;bottom:0;overflow:hidden;cursor:grab;touch-action:none}
#view.drag{cursor:grabbing}
#img{position:absolute;left:0;top:0;transform-origin:0 0;user-select:none;-webkit-user-drag:none;max-width:none}
#hint{position:absolute;right:12px;bottom:10px;background:#000c;padding:6px 10px;border-radius:6px;color:var(--dim);font-size:14px;pointer-events:none}
</style></head><body>
<div id="top"></div><div id="label"></div>
<div id="view"><img id="img" alt=""><div id="hint"></div></div>
<script>
const MAPS=__DATA__;
const top_=document.getElementById('top'),label=document.getElementById('label'),view=document.getElementById('view'),img=document.getElementById('img'),hint=document.getElementById('hint');
let cur=0,tod='day',s=1,x=0,y=0;
function apply(){img.style.transform=`translate(${x}px,${y}px) scale(${s})`;hint.textContent=`${Math.round(s*100)}%  ·  scroll to zoom at the cursor  ·  drag to move  ·  double-click to fit  ·  arrow keys change map  ·  N day or night`;}
function fit(){const r=view.getBoundingClientRect();s=Math.min(r.width/img.naturalWidth,r.height/img.naturalHeight);x=(r.width-img.naturalWidth*s)/2;y=(r.height-img.naturalHeight*s)/2;apply();}
function layout(){view.style.top=(top_.offsetHeight+label.offsetHeight)+'px';}
function show(i,keep){cur=(i+MAPS.length)%MAPS.length;const m=MAPS[cur];if(!m.pics[tod])tod=Object.keys(m.pics)[0];
 label.innerHTML=`${m.num}. ${m.name}<small>${tod}</small>`;
 [...top_.children].forEach((b,j)=>b.classList.toggle('on',j===cur));
 nb.disabled=Object.keys(m.pics).length<2;nb.textContent=tod==='day'?'Show night':'Show day';
 layout();img.onload=()=>{if(!keep)fit();else apply();};img.src=m.pics[tod];}
MAPS.forEach((m,i)=>{const b=document.createElement('button');b.textContent=m.num+'. '+m.name;b.onclick=()=>show(i);top_.appendChild(b);});
const nb=document.createElement('button');nb.onclick=()=>{tod=tod==='day'?'night':'day';show(cur,true);};
const holder=document.createElement('span');
view.addEventListener('wheel',e=>{e.preventDefault();const r=view.getBoundingClientRect(),px=e.clientX-r.left,py=e.clientY-r.top,f=Math.exp(-e.deltaY*0.0016),n=Math.min(40,Math.max(0.02,s*f));x=px-(px-x)*n/s;y=py-(py-y)*n/s;s=n;apply();},{passive:false});
let d=null;view.addEventListener('pointerdown',e=>{d={a:e.clientX,b:e.clientY,x,y};view.classList.add('drag');view.setPointerCapture(e.pointerId);});
view.addEventListener('pointermove',e=>{if(!d)return;x=d.x+e.clientX-d.a;y=d.y+e.clientY-d.b;apply();});
view.addEventListener('pointerup',()=>{d=null;view.classList.remove('drag');});
view.addEventListener('dblclick',fit);
addEventListener('keydown',e=>{if(e.key==='ArrowRight')show(cur+1);if(e.key==='ArrowLeft')show(cur-1);if(e.key==='n'||e.key==='N')nb.click();});
addEventListener('resize',()=>{layout();fit();});
top_.appendChild(nb);show(0);
</script></body></html>""".replace("__DATA__", data)
open(out, "w", encoding="utf-8").write(page)
print(out, round(os.path.getsize(out) / 1e6, 1), "MB", len(maps), "lands")
