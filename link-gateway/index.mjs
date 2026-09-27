import page from './network.html';
import {correlate} from './diagnose.mjs';
const SOURCES = ['windows', 'vmiss', 'home'];
const H = {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff', 'Referrer-Policy':'no-referrer'};
const json = (data, status=200) => new Response(JSON.stringify(data), {status, headers:{...H,'Content-Type':'application/json; charset=utf-8'}});
const bytes = value => Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')), x=>x.charCodeAt(0));
let keyCache = {until:0,issuer:'',keys:[]};

// Access still authenticates the edge, but the Worker also verifies every read.
// Missing configuration and alternate hostnames fail closed.
export async function authorized(request, env, fetcher=fetch) {
  if (!env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD || !env.ALLOWED_EMAIL) return false;
  if (!/^[a-z0-9-]+\.cloudflareaccess\.com$/.test(env.ACCESS_TEAM_DOMAIN)) return false;
  try {
    const token = request.headers.get('Cf-Access-Jwt-Assertion');
    if (!token || token.length>16000) return false;
    const parts=token.split('.');
    if(parts.length!==3) return false;
    const head=JSON.parse(new TextDecoder().decode(bytes(parts[0])));
    const claim=JSON.parse(new TextDecoder().decode(bytes(parts[1])));
    const issuer='https://'+env.ACCESS_TEAM_DOMAIN, now=Date.now()/1000;
    if(head.alg!=='RS256' || !head.kid || claim.iss!==issuer || !Array.isArray(claim.aud) ||
       !claim.aud.includes(env.ACCESS_AUD) || !Number.isFinite(claim.exp) || claim.exp<=now ||
       (claim.nbf && claim.nbf>now+30) || (claim.iat && claim.iat>now+30) ||
       String(claim.email).toLowerCase()!==env.ALLOWED_EMAIL.toLowerCase()) return false;
    if(keyCache.until<now || keyCache.issuer!==issuer || !keyCache.keys.some(k=>k.kid===head.kid)) {
      const response=await fetcher(issuer+'/cdn-cgi/access/certs',{signal:AbortSignal.timeout(5000)});
      if(!response.ok) return false;
      keyCache={until:now+600,issuer,keys:(await response.json()).keys};
    }
    const jwk=keyCache.keys.find(k=>k.kid===head.kid && k.kty==='RSA');
    if(!jwk) return false;
    const key=await crypto.subtle.importKey('jwk',jwk,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']);
    return await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,bytes(parts[2]),new TextEncoder().encode(parts[0]+'.'+parts[1]));
  } catch {return false;}
}
async function sameSecret(a,b) {
  if(!a || !b) return false;
  const digest=async x=>new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(x)));
  const [x,y]=await Promise.all([digest(a),digest(b)]);let result=0;
  for(let i=0;i<x.length;i++) result|=x[i]^y[i];return result===0;
}
export function validateBatch(data, source, now=Date.now()/1000) {
  if(data.source!==source || !Array.isArray(data.samples) || data.samples.length>60 ||
     !Array.isArray(data.incidents??[]) || (data.incidents??[]).length>10) throw Error('invalid batch');
  for(const s of data.samples) {
    if(s.source!==source || !/^[a-zA-Z0-9-]{8,64}$/.test(s.boot_id) ||
       !Number.isSafeInteger(s.seq) || s.seq<0 || !Number.isFinite(s.captured_at) ||
       s.captured_at>now+120 || s.captured_at<now-7*86400 || !Number.isFinite(s.monotonic_ms) ||
       typeof s.checks!=='object' || s.checks===null || JSON.stringify(s).length>24000) throw Error('invalid sample');
  }
  for(const i of data.incidents??[]) {
    if(i.source!==source || !/^[a-zA-Z0-9-]{8,80}$/.test(i.id) ||
       !Number.isFinite(i.started_at) || !Number.isFinite(i.end_at) ||
       i.end_at<i.started_at || i.end_at>i.started_at+3600 ||
       i.started_at>now+120 || i.started_at<now-30*86400 ||
       JSON.stringify(i).length>64000) throw Error('invalid incident');
  }
  return data;
}
function stub(env,source) {return env.PROBES.get(env.PROBES.idFromName(source));}
export default {async fetch(request,env) {
  const url=new URL(request.url),path=url.pathname;
  if(path==='/ingest' && request.method==='POST') {
    const source=request.headers.get('X-Probe-Source');
    let tokens;try{tokens=JSON.parse(env.PROBE_TOKENS||'{}');}catch{return json({error:'unconfigured'},503);}
    if(!SOURCES.includes(source) || !await sameSecret(request.headers.get('Authorization'), 'Bearer '+(tokens[source]||''))) return json({error:'unauthorized'},401);
    if(!tokens[source]) return json({error:'unauthorized'},401);
    if(Number(request.headers.get('Content-Length'))>262144) return json({error:'too large'},413);
    const body=await request.text();
    if(new TextEncoder().encode(body).length>262144) return json({error:'too large'},413);
    let data;
    try {data=validateBatch(JSON.parse(body),source);}catch{return json({error:'invalid batch'},400);}
    const response=await stub(env,source).fetch(new Request('https://internal/ingest',{method:'POST',body}));
    // A Windows incident requests matching server evidence via each server's next upload.
    // Probe tokens still have no read access to other sources' observations.
    if(source==='windows')for(const i of data.incidents??[]) {
      const target=i.target?.startsWith('vmiss.')?'vmiss':i.target?.startsWith('home.')?'home':null;
      if(target)for(const peer of ['vmiss','home'])await stub(env,peer).fetch(new Request('https://internal/collect-evidence',{method:'POST',
        body:JSON.stringify({id:i.id+'-'+peer,correlation_id:i.id,source:peer,target:peer===target?'server.local':target+'.hy2',
          started_at:i.started_at,end_at:i.end_at})}));
    }
    return response;
  }
  if(!await authorized(request,env)) return json({error:'login_required',message:'请通过 Cloudflare Access 登录；登录配置未完成时不提供链路数据。'},401);
  if(request.method!=='GET') return json({error:'method_not_allowed'},405);
  if(path==='/' || path==='/network' || path==='/network/') return new Response(page,{headers:{...H,'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"}});
  if(path==='/network/api/latest') {
    const results=await Promise.all(SOURCES.map(async source=>{
      try {return {source,...await (await stub(env,source).fetch('https://internal/latest')).json()};}
      catch{return {source,error:'storage_unavailable'};}
    }));
    return json({server_at:Date.now()/1000,probes:results,komari_url:env.KOMARI_URL||null});
  }
  if(path==='/network/api/incidents') {
    const groups=await Promise.all(SOURCES.map(async source=>(await (await stub(env,source).fetch('https://internal/incidents')).json()).incidents));
    const all=groups.flat();
    return json({incidents:all.filter(i=>!i.correlation_id).map(i=>({...i,report:correlate(i,all),
      related:all.filter(p=>p.correlation_id===i.id).map(p=>({source:p.source,id:p.id,status:p.status}))})).sort((a,b)=>b.started_at-a.started_at)});
  }
  if(path==='/network/api/history' || path==='/network/api/evidence') {
    const source=url.searchParams.get('source');
    if(!SOURCES.includes(source)) return json({error:'invalid source'},400);
    const internal=new URL('https://internal/'+path.split('/').pop());internal.search=url.search;
    const response=await stub(env,source).fetch(internal);
    if(path.endsWith('evidence') && source==='windows' && response.ok) {
      const data=await response.json(),id=url.searchParams.get('id');
      const related=await Promise.all(['vmiss','home'].map(async peer=>{
        const r=await stub(env,peer).fetch('https://internal/evidence?id='+encodeURIComponent(id+'-'+peer));
        return r.ok?await r.json():null;
      }));
      data.related=related.filter(Boolean);data.incident.report=correlate(data.incident,data.related.map(p=>p.incident));
      return new Response(JSON.stringify(data),{headers:{...H,'Content-Type':'application/json; charset=utf-8','Content-Disposition':'attachment; filename="correlated-evidence.json"'}});
    }
    const headers=new Headers(response.headers); for(const [k,v] of Object.entries(H)) headers.set(k,v);
    if(path.endsWith('evidence')) headers.set('Content-Disposition','attachment; filename="incident-evidence.json"');
    return new Response(response.body,{status:response.status,headers});
  }
  return json({error:'not_found'},404);
}};

export class ProbeStore {
  constructor(ctx) {
    this.ctx=ctx;this.sql=ctx.storage.sql;
    this.sql.exec('CREATE TABLE IF NOT EXISTS samples (boot TEXT,seq INTEGER,captured REAL,received REAL,body TEXT,PRIMARY KEY(boot,seq))');
    this.sql.exec('CREATE INDEX IF NOT EXISTS sample_time ON samples(captured)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS minutes (minute INTEGER,metric TEXT,n INTEGER,failed INTEGER,total REAL,lo REAL,hi REAL,PRIMARY KEY(minute,metric))');
    this.sql.exec('CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY,started REAL,ends REAL,body TEXT)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS evidence (incident TEXT,boot TEXT,seq INTEGER,body TEXT,PRIMARY KEY(incident,boot,seq))');
    this.sql.exec('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,created REAL,body TEXT,done INTEGER DEFAULT 0)');
  }
  rows(query,...args) {return [...this.sql.exec(query,...args)];}
  async fetch(request) {
    const u=new URL(request.url),now=Date.now()/1000;
    if(u.pathname==='/collect-evidence') {
      const job=await request.json();
      if(this.rows('SELECT 1 FROM jobs WHERE created>? AND id<>? LIMIT 1',job.started_at-600,job.id).length) return json({queued:false,reason:'cooldown'});
      this.sql.exec('INSERT OR IGNORE INTO jobs(id,created,body) VALUES(?,?,?)',job.id,job.started_at,JSON.stringify(job));
      return json({queued:true});
    }
    if(u.pathname==='/ingest') {
      const data=await request.json();let inserted=0;
      this.ctx.storage.transactionSync(()=>{
        for(const s of data.samples) {
          if(this.rows('SELECT 1 FROM samples WHERE boot=? AND seq=?',s.boot_id,s.seq).length) continue;
          const body=JSON.stringify({...s,received_at:now});
          this.sql.exec('INSERT INTO samples VALUES(?,?,?,?,?)',s.boot_id,s.seq,s.captured_at,now,body);inserted++;
          for(const [metric,c] of Object.entries(s.checks)) {
            if(!['ok','fail'].includes(c?.state) || c.new===false) continue; // missing observations never count as loss
            const ms=c.state==='ok' && Number.isFinite(c.ms)?c.ms:null;
            this.sql.exec('INSERT INTO minutes VALUES(?,?,?,?,?,?,?) ON CONFLICT(minute,metric) DO UPDATE SET n=n+1,failed=failed+excluded.failed,total=total+excluded.total,lo=min(lo,excluded.lo),hi=max(hi,excluded.hi)',
              Math.floor(s.captured_at/60)*60,metric,1,c.state==='fail'?1:0,ms||0,ms,ms);
          }
          for(const i of this.rows('SELECT id FROM incidents WHERE started-600<=? AND ends>=?',s.captured_at,s.captured_at))
            this.sql.exec('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)',i.id,s.boot_id,s.seq,body);
        }
        for(const i of data.incidents??[]) {
          this.sql.exec('INSERT INTO incidents VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET ends=excluded.ends,body=excluded.body',i.id,i.started_at,i.end_at,JSON.stringify(i));
          this.sql.exec('INSERT OR IGNORE INTO evidence SELECT ?,boot,seq,body FROM samples WHERE captured BETWEEN ? AND ?',i.id,i.started_at-600,i.end_at);
          this.sql.exec('UPDATE jobs SET done=1 WHERE id=?',i.id);
        }
        this.sql.exec("INSERT OR REPLACE INTO meta VALUES('last_receive',?)",String(now));
      });
      if(!await this.ctx.storage.getAlarm()) await this.ctx.storage.setAlarm(Date.now()+3600000);
      return json({accepted:inserted,server_at:now,requests:this.rows('SELECT body FROM jobs WHERE done=0 ORDER BY created LIMIT 5').map(r=>JSON.parse(r.body))});
    }
    if(u.pathname==='/latest') {
      const latest=this.rows('SELECT body FROM samples ORDER BY captured DESC,received DESC LIMIT 1')[0];
      const r=this.rows("SELECT value FROM meta WHERE key='last_receive'")[0];
      return json({sample:latest?JSON.parse(latest.body):null,last_receive:r?Number(r.value):null,
        retention_warnings:this.rows("SELECT key,value FROM meta WHERE key LIKE '%retention_warning'")});
    }
    if(u.pathname==='/history') {
      const start=Math.max(now-30*86400,Number(u.searchParams.get('start'))||now-3600);
      const end=Math.min(now+120,Number(u.searchParams.get('end'))||now);
      if(end<start) return json({error:'invalid time'},400);
      // Minute data keeps a 30 day window and reduces chart payloads.
      const bucket=end-start>7*86400?1800:end-start>86400?300:60;
      return json({bucket_seconds:bucket,minutes:this.rows('SELECT CAST(minute/? AS INTEGER)*? AS minute,metric,SUM(n) AS n,SUM(failed) AS failed,SUM(total) AS total,MIN(lo) AS lo,MAX(hi) AS hi FROM minutes WHERE minute BETWEEN ? AND ? GROUP BY CAST(minute/? AS INTEGER),metric ORDER BY minute LIMIT 50000',bucket,bucket,start,end,bucket)});
    }
    if(u.pathname==='/incidents') return json({incidents:this.rows('SELECT body FROM incidents ORDER BY started DESC LIMIT 100').map(r=>JSON.parse(r.body))});
    if(u.pathname==='/evidence') {
      const id=u.searchParams.get('id'),i=this.rows('SELECT body FROM incidents WHERE id=?',id)[0];
      if(!i) return json({error:'not_found'},404);
      return json({incident:JSON.parse(i.body),retention_warnings:this.rows("SELECT key,value FROM meta WHERE key LIKE '%retention_warning'"),samples:this.rows('SELECT body FROM evidence WHERE incident=? ORDER BY json_extract(body,\'$.captured_at\') LIMIT 3000',id).map(r=>JSON.parse(r.body))});
    }
    return json({error:'not_found'},404);
  }
  async alarm() {
    const now=Date.now()/1000;
    this.sql.exec('DELETE FROM samples WHERE captured<?',now-7*86400);
    this.sql.exec('DELETE FROM minutes WHERE minute<?',now-30*86400);
    this.sql.exec('DELETE FROM incidents WHERE started<?',now-30*86400);
    this.sql.exec('DELETE FROM evidence WHERE incident NOT IN (SELECT id FROM incidents)');
    this.sql.exec('DELETE FROM jobs WHERE created<?',now-30*86400);
    // Explicit high-water bound per source. Capacity trimming is visible in diagnostics.
    const size=this.rows('SELECT COALESCE(SUM(length(body)),0) AS n FROM samples')[0].n;
    if(size>700*1024*1024) {
      this.sql.exec('DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples ORDER BY captured LIMIT 20000)');
      this.sql.exec("INSERT OR REPLACE INTO meta VALUES('retention_warning',?)",String(now));
    }
    const evidenceSize=this.rows('SELECT COALESCE(SUM(length(body)),0) AS n FROM evidence')[0].n;
    if(evidenceSize>100*1024*1024) {
      this.sql.exec('DELETE FROM evidence WHERE incident IN (SELECT id FROM incidents ORDER BY started LIMIT 20)');
      this.sql.exec("INSERT OR REPLACE INTO meta VALUES('evidence_retention_warning',?)",String(now));
    }
    await this.ctx.storage.setAlarm(Date.now()+3600000);
  }
}
