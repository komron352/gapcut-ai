import os, re, json, time, uuid, shutil, asyncio, tempfile, subprocess, logging, hmac, hashlib
from pathlib import Path
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager
import httpx, uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

load_dotenv()
VERSION="4.0.0"
BOT_TOKEN=os.getenv("BOT_TOKEN","")
GROQ_API_KEY=os.getenv("GROQ_API_KEY","")
PORT=int(os.getenv("PORT","10000"))
APP_URL=os.getenv("MINI_APP_URL","https://gapcut-ai-web.vercel.app")
SUPPORT=os.getenv("SUPPORT_URL","https://t.me/aikomron")
CHANNEL=os.getenv("CHANNEL_URL","https://t.me/gapcutai")
GROQ="https://api.groq.com/openai/v1"
WHISPER=os.getenv("WHISPER_MODEL","whisper-large-v3-turbo")
LLM=os.getenv("TEXT_MODEL","llama-3.3-70b-versatile")
MAX_INPUT=100*1024*1024
MAX_OUTPUT=150*1024*1024
MAX_DURATION=int(os.getenv("MAX_DURATION","180"))
ROOT=Path(tempfile.gettempdir())/"gapcut4"; ROOT.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO)
log=logging.getLogger("gapcut")
bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if BOT_TOKEN else None
dp=Dispatcher()
JOBS={}
TASKS={}
LOCK=asyncio.Lock()
STYLES={"viral":(58,1),"classic":(48,0),"cinema":(50,1),"podcast":(48,1),"karaoke":(56,1),"creator":(54,1),"minimal":(44,0)}
FORMATS={"9:16":(1080,1920),"16:9":(1920,1080),"1:1":(1080,1080)}
FONTS=["DejaVu Sans","DejaVu Sans Condensed","Liberation Sans","Noto Sans"]

def cmd(args,timeout=900):
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
    if p.returncode: raise RuntimeError((p.stderr or "FFmpeg error")[-2500:])
    return p.stdout

def auth(init):
    if not BOT_TOKEN or not init: raise HTTPException(401,"Telegram initData лозим аст.")
    v=dict(parse_qsl(init,keep_blank_values=True)); received=v.pop("hash","")
    check="\n".join(f"{k}={val}" for k,val in sorted(v.items()))
    secret=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received,expected): raise HTTPException(401,"Telegram auth нодуруст аст.")
    if time.time()-int(v.get("auth_date","0"))>86400: raise HTTPException(401,"Сессия гузаштааст.")
    try:return str(json.loads(v["user"])["id"])
    except Exception:raise HTTPException(401,"Маълумоти Telegram нодуруст аст.")

async def groq(endpoint,data=None,files=None,timeout=240):
    if not GROQ_API_KEY: raise RuntimeError("GROQ_API_KEY дар Render насб нашудааст.")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r=await c.post(GROQ+"/"+endpoint,headers={"Authorization":"Bearer "+GROQ_API_KEY},data=data,files=files)
    if r.status_code>=400: raise RuntimeError(f"Groq {r.status_code}: {r.text[:1000]}")
    return r.json()

def probe(path):
    raw=cmd(["ffprobe","-v","error","-show_entries","format=duration:stream=codec_type,width,height","-of","json",str(path)],60)
    d=json.loads(raw); s=d.get("streams",[])
    v=next((x for x in s if x.get("codec_type")=="video"),None)
    if not v: raise ValueError("Файл видеои дуруст нест.")
    return {"duration":float(d["format"]["duration"]),"w":int(v["width"]),"h":int(v["height"]),
            "audio":any(x.get("codec_type")=="audio" for x in s)}

async def transcribe(path):
    wav=path.parent/"audio.wav"
    cmd(["ffmpeg","-y","-i",str(path),"-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(wav)],300)
    if wav.stat().st_size>24*1024*1024:
        mp3=path.parent/"audio.mp3"; cmd(["ffmpeg","-y","-i",str(wav),"-b:a","48k",str(mp3)],300); wav=mp3
    with wav.open("rb") as f:
        data=await groq("audio/transcriptions",{"model":WHISPER,"response_format":"verbose_json",
            "timestamp_granularities[]":"word","prompt":"Transcribe exactly in Tajik Cyrillic or Russian, including mixed speech. Preserve ғ ӣ қ ӯ ҳ ҷ. Do not translate."},
            {"file":(wav.name,f,"audio/wav")})
    words=[]
    for x in data.get("words",[]):
        t=str(x.get("word","")).strip()
        if t: words.append({"word":t,"start":max(0,float(x.get("start",0))),"end":max(.1,float(x.get("end",.1))),"key":False})
    if not words:
        for i,t in enumerate(data.get("text","").split()): words.append({"word":t,"start":i*.4,"end":i*.4+.35,"key":False})
    return words

async def correct(words):
    if not words:return words
    original=" ".join(w["word"] for w in words)
    prompt=("Correct only obvious Tajik/Russian ASR spelling and punctuation. Keep original meaning, "
            "word order, language switching, names. Do not translate or add/remove words. Preserve Tajik ғ ӣ қ ӯ ҳ ҷ. Return only text.")
    try:
        r=await groq("chat/completions",data={"model":LLM,"temperature":"0.1",
            "messages":json.dumps([{"role":"system","content":prompt},{"role":"user","content":original}],ensure_ascii=False)},timeout=90)
        tokens=r["choices"][0]["message"]["content"].strip().split()
        if len(tokens)==len(words): return [{**w,"word":t} for w,t in zip(words,tokens)]
    except Exception as e: log.warning("Correction skipped: %s",e)
    return words

def groups(words):
    result=[]; cur=[]
    for w in words:
        if cur and (len(cur)>=5 or w["end"]-cur[0]["start"]>2.7 or w["start"]-cur[-1]["end"]>.65):
            result.append(cur);cur=[]
        cur.append(w)
        if re.search(r"[.!?…]$",w["word"]):result.append(cur);cur=[]
    if cur:result.append(cur)
    return result

def atime(s):
    n=max(0,int(s*100));return f"{n//360000}:{(n//6000)%60:02}:{(n//100)%60:02}.{n%100:02}"

def ass_file(words,opt,info,path):
    fmt=opt["format"]; tw,th=FORMATS.get(fmt,(info["w"],info["h"]))
    size=max(24,min(90,int(opt["font_size"])))
    align=8 if opt["position"]=="top" else 2
    margin=int(th*.13)
    style=STYLES[opt["style"]]
    primary=opt["color"]; accent=opt["accent"]
    out=["[Script Info]","ScriptType: v4.00+","WrapStyle: 2",f"PlayResX: {tw}",f"PlayResY: {th}","",
    "[V4+ Styles]","Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    f"Style: Default,{opt['font']},{size},{primary},&H000000FF,&H00000000,&H80000000,{style[1]},0,1,3,1,{align},50,50,{margin},1","",
    "[Events]","Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for line in groups(words):
        text=[]
        for w in line:
            t=w["word"].replace("\\",r"\\").replace("{",r"\{").replace("}",r"\}")
            if re.search(r"\d|[A-ZА-ЯҒӢҚӮҲҶ]{2,}",t):
                text.append(r"{\c"+accent+r"\b1}"+t+r"{\c"+primary+r"\b1}")
            else:text.append(t)
        caption=" ".join(text)
        if opt["style"] in ("viral","creator","karaoke"):caption=r"{\fad(90,70)\fscx96\t(0,120,\fscx104)}"+caption
        elif opt["style"]=="cinema":caption=r"{\fad(180,180)}"+caption
        out.append(f"Dialogue: 0,{atime(line[0]['start'])},{atime(max(line[-1]['end'],line[0]['start']+.15))},Default,,0,0,0,,{caption}")
    path.write_text("\n".join(out),encoding="utf-8-sig")
    return tw,th,path

def render(job):
    src=job["input"]; out=job["dir"]/"GAPCUT_AI_4_PRO.mp4";opt=job["options"];info=job["info"]
    tw,th,ass=ass_file(job["words"],opt,info,job["dir"]/"subs.ass")
    ap=str(ass).replace("\\","/").replace(":","\\:")
    filt=f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1,fps=30"
    if opt["zoom"]:filt+=",scale=iw*1.025:ih*1.025,crop=iw/1.025:ih/1.025"
    if opt["flash"]:filt+=",eq=contrast=1.04:saturation=1.10"
    if opt["subtitles"]:filt+=f",subtitles='{ap}'"
    args=["ffmpeg","-y","-i",str(src),"-vf",filt,"-c:v","libx264","-preset",os.getenv("FFMPEG_PRESET","veryfast"),
          "-crf","21","-pix_fmt","yuv420p","-c:a","aac","-b:a","160k","-movflags","+faststart",str(out)]
    cmd(args,1200)
    if out.stat().st_size>MAX_OUTPUT:raise RuntimeError("Файли натиҷа аз 150 MB зиёд шуд.")
    return out

def pub(j):
    return {k:j.get(k) for k in ("id","status","progress","message","transcript","words","duration","error")}

async def transcribe_job(j):
    try:
        j.update(status="transcribing",progress=10,message="Шинохти овоз...")
        async with LOCK: words=await transcribe(j["input"])
        j["progress"]=65;j["message"]="Ислоҳи имло..."
        words=await correct(words);j["words"]=words;j["transcript"]=" ".join(x["word"] for x in words)
        j.update(status="ready",progress=100,message="Матн тайёр аст. Санҷ ва монтажро оғоз кун.")
    except Exception as e:
        log.exception("Transcription error");j.update(status="error",error=str(e)[:1200],message="Хатогӣ дар шинохти овоз.")

async def render_job(j):
    try:
        j.update(status="rendering",progress=8,message="Монтаж сар шуд...")
        async with LOCK: out=await asyncio.to_thread(render,j)
        j["output"]=str(out);j.update(status="done",progress=100,message="Видео тайёр аст!")
    except Exception as e:
        log.exception("Render error");j.update(status="error",error=str(e)[-1200:],message="Хатогӣ дар монтаж.")

def task(coro,jid):
    t=asyncio.create_task(coro);TASKS[jid]=t;t.add_done_callback(lambda _:TASKS.pop(jid,None))

@asynccontextmanager
async def lifespan(app):
    Path(ROOT).mkdir(exist_ok=True)
    if bot:asyncio.create_task(dp.start_polling(bot))
    yield
    if bot:await bot.session.close()

app=FastAPI(title="GAPCUT AI 4.0 PRO AUTO",version=VERSION,lifespan=lifespan)
origins=[x.strip() for x in os.getenv("ALLOWED_ORIGINS",APP_URL).split(",") if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins or ["*"],allow_methods=["*"],allow_headers=["*"])

def get_user(authorization):return auth((authorization or "").removeprefix("tma "))
def owned(jid,uid):
    j=JOBS.get(jid)
    if not j:raise HTTPException(404,"Лоиҳа ёфт нашуд.")
    if j["user"]!=uid:raise HTTPException(403,"Дастрасӣ манъ аст.")
    return j

@app.get("/")
async def home():return {"name":"GAPCUT AI","version":VERSION,"status":"online"}
@app.get("/health")
async def health():return {"ok":True,"version":VERSION,"ffmpeg":bool(shutil.which("ffmpeg")),"groq":bool(GROQ_API_KEY)}
@app.get("/api/fonts")
async def fonts():return {"fonts":FONTS}

@app.post("/api/jobs")
async def create(file:UploadFile=File(...),authorization:str|None=Header(default=None),
 style:str=Form("viral"),format:str=Form("9:16"),language:str=Form("both"),font:str=Form("DejaVu Sans"),
 font_size:int=Form(54),position:str=Form("bottom"),color:str=Form("&H00FFFFFF"),accent:str=Form("&H0000D7FF"),
 zoom:str=Form("true"),flash:str=Form("false"),cards:str=Form("false"),subtitles:str=Form("true")):
    uid=get_user(authorization)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):raise HTTPException(503,"FFmpeg дар сервер насб нест.")
    if Path(file.filename or "").suffix.lower() not in {".mp4",".mov",".mkv",".webm",".m4v"}:raise HTTPException(400,"Формати видео нодуруст аст.")
    jid=uuid.uuid4().hex[:12];folder=ROOT/jid;folder.mkdir()
    src=folder/("input"+Path(file.filename).suffix.lower());total=0
    try:
        with src.open("wb") as f:
            while chunk:=await file.read(1024*1024):
                total+=len(chunk)
                if total>MAX_INPUT:raise HTTPException(413,"Видео аз 100 MB зиёд аст.")
                f.write(chunk)
        info=await asyncio.to_thread(probe,src)
        if info["duration"]<=0 or info["duration"]>MAX_DURATION:raise HTTPException(400,f"Видео бояд то {MAX_DURATION} сония бошад.")
        style=style if style in STYLES else "viral";format=format if format in {*FORMATS,"original"} else "9:16"
        language=language if language in {"tg","ru","both","auto"} else "both"
        color=color if re.fullmatch(r"&H[0-9A-Fa-f]{8}",color) else "&H00FFFFFF"
        accent=accent if re.fullmatch(r"&H[0-9A-Fa-f]{8}",accent) else "&H0000D7FF"
        opt={"style":style,"format":format,"language":language,"font":font if font in FONTS else "DejaVu Sans",
             "font_size":max(24,min(90,font_size)),"position":position if position in {"top","bottom"} else "bottom",
             "color":color,"accent":accent,"zoom":zoom.lower() not in {"false","0"},
             "flash":flash.lower() in {"true","1"},"subtitles":subtitles.lower() not in {"false","0"}}
        j={"id":jid,"user":uid,"dir":folder,"input":src,"info":info,"duration":info["duration"],"options":opt,
           "status":"queued","progress":0,"message":"Видео қабул шуд.","created":time.time(),"words":[],"transcript":""}
        JOBS[jid]=j;task(transcribe_job(j),jid);return pub(j)
    except Exception:
        shutil.rmtree(folder,ignore_errors=True);raise

@app.get("/api/jobs/{jid}")
async def status(jid:str,authorization:str|None=Header(default=None)):
    return pub(owned(jid,get_user(authorization)))

@app.post("/api/jobs/{jid}/render")
async def start_render(jid:str,authorization:str|None=Header(default=None),transcript:str=Form("")):
    j=owned(jid,get_user(authorization))
    if j["status"]!="ready":raise HTTPException(409,"Шинохти овоз ҳоло анҷом наёфтааст.")
    if transcript.strip():
        old=j["words"];tokens=transcript.split()
        if len(tokens)==len(old):j["words"]=[{**w,"word":t} for w,t in zip(old,tokens)]
        else:
            n=len(tokens);dur=j["duration"]
            j["words"]=[{"word":t,"start":i*dur/n,"end":min(dur,(i+1)*dur/n-.01),"key":False} for i,t in enumerate(tokens)]
    task(render_job(j),jid);return pub(j)

@app.get("/api/jobs/{jid}/download")
async def download(jid:str,authorization:str|None=Header(default=None)):
    j=owned(jid,get_user(authorization));p=j.get("output")
    if j["status"]!="done" or not p or not Path(p).exists():raise HTTPException(409,"Видео ҳоло тайёр нест.")
    return FileResponse(p,media_type="video/mp4",filename="GAPCUT_AI_4_PRO.mp4")

@app.delete("/api/jobs/{jid}")
async def delete(jid:str,authorization:str|None=Header(default=None)):
    j=owned(jid,get_user(authorization))
    if jid in TASKS:TASKS[jid].cancel()
    shutil.rmtree(j["dir"],ignore_errors=True);JOBS.pop(jid,None);return {"ok":True}

@dp.message(CommandStart())
async def start(message:Message):
    kb=InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="🎬 Оғози монтаж",web_app=WebAppInfo(url=APP_URL))],
      [InlineKeyboardButton(text="📢 Канал",url=CHANNEL),InlineKeyboardButton(text="💬 Дастгирӣ",url=SUPPORT)]])
    await message.answer("<b>GAPCUT AI 4.0 PRO AUTO</b>\n\nВидео бор кун ва монтажро ба AI супор.",reply_markup=kb)
@dp.message(F.video|F.document)
async def video_hint(message:Message):
    await message.answer("Барои монтаж тугмаи «Оғози монтаж»-ро пахш кун.")
if __name__=="__main__":uvicorn.run("bot:app",host="0.0.0.0",port=PORT)
