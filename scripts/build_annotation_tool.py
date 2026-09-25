"""生成本地标注网页(SPEC §22–§26)。纯静态 HTML,离线打开,不上传任何内容。

    python scripts/build_annotation_tool.py

输出到 outputs/interaction_experiment/annotation/(含正文,不进仓):
  annotate_practice.html  20 条练习,两人一起标、一起讨论,不计入信度
  annotate_A.html         标注员 A:全部 500 条
  annotate_B.html         标注员 B:信度子集 150 条(与 A 独立完成,完成前不讨论)

网页里看不到任何系统判定;进度存在浏览器 localStorage,「导出 CSV」下载结果。
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

OUT = Path("outputs/interaction_experiment/annotation")
ROLES = [("automated_feed", "自动 feed"), ("organizational_broadcast", "机构官方号"),
         ("news_media", "新闻媒体"), ("security_professional", "安全从业者"),
         ("individual_participant", "普通个人"), ("unclear_other", "无法判断")]
FUNCS = [("broadcast_or_repost", "单向发布/转贴"), ("substantive_reply", "实质回应"),
         ("information_seeking", "提问/求证"), ("information_providing", "提供信息"),
         ("discussion_or_reaction", "表态/反应"), ("unclear", "无法判断")]
TYPE_ZH = {"seed": "种子帖", "reply_d1": "回复(深度1)", "reply_d2": "回复(深度2)", "quote": "引用帖"}

PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>__TITLE__</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:20px auto;padding:0 16px;color:#222}
.ctx{background:#f4f4f4;border-left:4px solid #bbb;padding:8px 12px;margin:6px 0;white-space:pre-wrap}
.cur{background:#fff8dc;border-left:4px solid #e0a800;padding:10px 12px;margin:6px 0;white-space:pre-wrap;font-size:1.05em}
.lab{font-size:.8em;color:#666}
fieldset{border:1px solid #ddd;margin:10px 0;padding:6px 10px}
label{display:inline-block;margin:3px 10px 3px 0;cursor:pointer}
kbd{background:#eee;border:1px solid #ccc;border-radius:3px;padding:0 4px;font-size:.8em}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
button{padding:6px 12px}.done{color:green}.todo{color:#c00}
textarea{width:100%;height:50px}
</style></head><body>
<h2>__TITLE__</h2>
<div class="bar">标注员 ID:<input id="who" size="8" placeholder="例如 xw">
<span id="prog"></span>
<button onclick="go(-1)">← 上一条</button><button onclick="go(1)">下一条 →</button>
<button onclick="nextTodo()">跳到未完成</button>
<button onclick="exportCsv()">导出 CSV</button>
<label>导入(续标)<input type="file" accept=".csv" onchange="importCsv(this.files[0])"></label></div>
<div id="card"></div>
<p class="lab">快捷键：角色 <kbd>1</kbd>–<kbd>6</kbd>，功能 <kbd>Q</kbd><kbd>W</kbd><kbd>E</kbd><kbd>R</kbd><kbd>T</kbd><kbd>Y</kbd>，
把握度 <kbd>H</kbd>/<kbd>M</kbd>/<kbd>L</kbd>，<kbd>→</kbd>/<kbd>←</kbd> 翻页（在备注框里输入时快捷键不生效）。
进度自动保存在本浏览器；换电脑或清缓存前先导出 CSV。</p>
<script>
const ITEMS=__ITEMS__, ROLES=__ROLES__, FUNCS=__FUNCS__, KEY="__KEY__";
let S={}; try{S=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){}
let i=0; try{i=+localStorage.getItem(KEY+":pos")||0}catch(e){}
const who=document.getElementById("who"); try{who.value=localStorage.getItem(KEY+":who")||""}catch(e){}
who.oninput=()=>{try{localStorage.setItem(KEY+":who",who.value)}catch(e){}};
function save(){try{localStorage.setItem(KEY,JSON.stringify(S));localStorage.setItem(KEY+":pos",i)}catch(e){}}
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
function complete(a){return a&&a.role&&a.function&&a.confidence}
function render(){
 const it=ITEMS[i], a=S[it.item_id]||{};
 let h=`<p><b>${it.item_id}</b> · ${it.type_zh} · 作者 <b>${esc(it.author_handle)}</b> ${esc(it.author_display_name)}
  · <a href="${it.post_url}" target="_blank">打开原帖</a></p>`;
 if(it.context_root) h+=`<div class="lab">root（对话起点）</div><div class="ctx">${esc(it.context_root)}</div>`;
 if(it.context_parent && it.context_parent!==it.context_root)
   h+=`<div class="lab">${it.unit_type==="quote"?"被引用的帖子":"父帖（直接回复的对象）"}</div><div class="ctx">${esc(it.context_parent)}</div>`;
 h+=`<div class="lab">▼ 要标注的这一条</div><div class="cur">${esc(it.text)||"（无正文）"}</div>`;
 const grp=(name,opts,keys)=>opts.map((o,k)=>`<label><input type="radio" name="${name}" value="${o[0]}" ${a[name]===o[0]?"checked":""}
   onchange="setv('${name}',this.value)"> <kbd>${keys[k]}</kbd> ${o[1]} <span class="lab">${o[0]}</span></label>`).join("");
 h+=`<fieldset><legend>维度 A：作者是谁（role）</legend>${grp("role",ROLES,"123456")}</fieldset>`;
 h+=`<fieldset><legend>维度 B：这一条在做什么（function）</legend>${grp("function",FUNCS,"QWERTY")}</fieldset>`;
 h+=`<fieldset><legend>把握度</legend>${grp("confidence",[["high","高"],["medium","中"],["low","低"]],"HML")}</fieldset>`;
 h+=`<textarea placeholder="备注（可选：次要功能、拿不准的理由）" oninput="setv('notes',this.value)">${esc(a.notes)}</textarea>`;
 document.getElementById("card").innerHTML=h;
 const n=ITEMS.filter(x=>complete(S[x.item_id])).length;
 document.getElementById("prog").innerHTML=`第 ${i+1}/${ITEMS.length} 条 · 已完成 ${n} · `+
   (complete(a)?'<span class="done">本条已完成</span>':'<span class="todo">本条未完成</span>');
}
function setv(k,v){const id=ITEMS[i].item_id;S[id]=Object.assign(S[id]||{},{[k]:v});save();
 if(k!=="notes")render()}
function go(d){i=Math.max(0,Math.min(ITEMS.length-1,i+d));save();render();window.scrollTo(0,0)}
function nextTodo(){for(let k=1;k<=ITEMS.length;k++){const j=(i+k)%ITEMS.length;if(!complete(S[ITEMS[j].item_id])){i=j;save();render();return}}alert("全部完成")}
document.onkeydown=e=>{if(e.target.tagName==="TEXTAREA"||e.target.tagName==="INPUT"&&e.target.type!=="radio")return;
 const k=e.key.toLowerCase(), r="123456".indexOf(k), f="qwerty".indexOf(k), c="hml".indexOf(k);
 if(r>=0)setv("role",ROLES[r][0]); else if(f>=0)setv("function",FUNCS[f][0]);
 else if(c>=0)setv("confidence",["high","medium","low"][c]);
 else if(e.key==="ArrowRight")go(1); else if(e.key==="ArrowLeft")go(-1)};
function csvq(s){s=(s||"").toString();return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s}
function exportCsv(){if(!who.value){alert("先填标注员 ID");return}
 const rows=[["item_id","annotator","role","function","confidence","notes"]];
 ITEMS.forEach(x=>{const a=S[x.item_id]||{};rows.push([x.item_id,who.value,a.role,a.function,a.confidence,a.notes])});
 const b=new Blob([rows.map(r=>r.map(csvq).join(",")).join("\n")],{type:"text/csv"});
 const l=document.createElement("a");l.href=URL.createObjectURL(b);l.download=`${KEY}_${who.value}.csv`;l.click()}
function importCsv(file){const rd=new FileReader();rd.onload=()=>{
 const lines=rd.result.split(/\r?\n/).slice(1);let n=0;
 for(const line of lines){const m=[];let cur="",q=false;
  for(let k=0;k<line.length;k++){const ch=line[k];
   if(q){if(ch=='"'&&line[k+1]=='"'){cur+='"';k++}else if(ch=='"')q=false;else cur+=ch}
   else if(ch=='"')q=true;else if(ch==","){m.push(cur);cur=""}else cur+=ch}
  m.push(cur); if(m[0]){S[m[0]]={role:m[2],function:m[3],confidence:m[4],notes:m[5]};n++}}
 save();render();alert(`导入 ${n} 条`)};rd.readAsText(file)}
render();
</script></body></html>"""


def build(name: str, title: str, items: list[dict]) -> None:
    data = [{**{k: it[k] for k in ("item_id", "unit_type", "context_root", "context_parent",
                                   "text", "author_handle", "author_display_name", "post_url")},
             "type_zh": TYPE_ZH[it["unit_type"]]} for it in items]
    js = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")
    html = (PAGE.replace("__TITLE__", title).replace("__ITEMS__", js(data))
            .replace("__ROLES__", js(ROLES)).replace("__FUNCS__", js(FUNCS))
            .replace("__KEY__", f"ccint_annot_{name}"))
    (OUT / f"annotate_{name}.html").write_text(html)
    print(f"annotate_{name}.html: {len(items)} items")


def main() -> None:
    items = list(csv.DictReader((OUT / "annotation_items.csv").open()))
    keys = {r["item_id"]: r for r in csv.DictReader((OUT / "annotation_key.csv").open())}
    rel = [it for it in items if keys[it["item_id"]]["reliability_subset"] == "True"]
    rng = random.Random(20260927)
    # 练习集:不在信度子集里,四种单元类型各 5 条
    practice = []
    for t in ("seed", "reply_d1", "reply_d2", "quote"):
        pool = [it for it in items if it["unit_type"] == t and keys[it["item_id"]]["reliability_subset"] == "False"]
        practice += rng.sample(pool, 5)
    build("practice", "ccint 标注 · 练习（20 条，两人一起）", practice)
    build("A", "ccint 标注 · 标注员 A（500 条）", items)
    build("B", "ccint 标注 · 标注员 B（信度子集 150 条）", rel)
    (OUT / "practice_item_ids.txt").write_text("\n".join(it["item_id"] for it in practice))


if __name__ == "__main__":
    main()
