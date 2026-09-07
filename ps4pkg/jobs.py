"""Fila de downloads e envios, persistida em disco."""
import json
import threading
import time
import uuid
from pathlib import Path

from . import ps4
from .config import STATE_DIR, settings
from .engine import Cancelled, Download, filename_for

JOBS_FILE = STATE_DIR / "jobs.json"
ACTIVE = ("queued", "downloading", "transferring", "installing")


class Manager:
    def __init__(self):
        self.jobs = {}
        self.order = []
        self._lock = threading.RLock()
        self._live = {}          # job_id -> Download em andamento
        self._cancels = {}       # job_id -> Event (envios FTP)
        self._wake = threading.Event()
        self._load()
        threading.Thread(target=self._loop, daemon=True, name="queue").start()

    # ---------- persistencia ----------

    def _load(self):
        if not JOBS_FILE.exists():
            return
        try:
            data = json.loads(JOBS_FILE.read_text())
        except Exception:
            return
        for j in data.get("jobs", []):
            # nada continua "rodando" depois de um restart do servidor
            if j.get("status") in ("downloading", "transferring", "installing"):
                j["status"] = "queued"
            self.jobs[j["id"]] = j
        self.order = [i for i in data.get("order", []) if i in self.jobs]

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = {"order": list(self.order),
                       "jobs": [self.jobs[i] for i in self.order if i in self.jobs]}
        tmp = JOBS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(JOBS_FILE)

    # ---------- api publica ----------

    def add_download(self, item, user_agent=None, connections=None):
        url = item["url"]
        with self._lock:
            for j in self.jobs.values():
                if j["type"] == "download" and j["url"] == url and j["status"] in ACTIVE:
                    return j, False
            jid = uuid.uuid4().hex[:12]
            job = {
                "id": jid, "type": "download", "url": url,
                "name": item.get("name") or filename_for(url),
                "filename": item.get("filename") or filename_for(url),
                "size": int(item.get("size") or 0),
                "title_id": item.get("title_id", ""),
                "region": item.get("region", ""),
                "kind": item.get("kind", "games"),
                "cover_url": item.get("cover_url", ""),
                "user_agent": user_agent,
                "connections": connections,
                "source": item.get("source", ""),
                "status": "queued", "error": None,
                "progress": {"downloaded": 0, "size": int(item.get("size") or 0),
                             "percent": 0.0, "speed": 0, "eta": None},
                "added_at": time.time(), "finished_at": None,
            }
            self.jobs[jid] = job
            self.order.append(jid)
        self._save()
        self._wake.set()
        return job, True

    def add_transfer(self, filename, destination=None, delete_after=None):
        """Enfileira o envio de um pacote ja baixado pro destino final."""
        path = settings.dest / Path(filename).name
        if not path.exists():
            raise FileNotFoundError(filename)
        remote_dir = ps4.resolve_destination(destination)
        name = next((d["name"] for d in ps4.destinations()
                     if d["path"] == remote_dir), remote_dir)
        size = path.stat().st_size
        with self._lock:
            for j in self.jobs.values():
                if (j["type"] == "transfer" and j["filename"] == path.name
                        and j.get("remote_dir") == remote_dir
                        and j["status"] in ACTIVE):
                    return j, False
            jid = uuid.uuid4().hex[:12]
            job = {
                "id": jid, "type": "transfer", "url": None,
                "name": path.name, "filename": path.name, "size": size,
                "remote_dir": remote_dir, "dest_name": name,
                "delete_after": (settings["delete_after_transfer"]
                                 if delete_after is None else bool(delete_after)),
                "title_id": "", "region": "", "kind": "transfer", "cover_url": "",
                "status": "queued", "error": None, "result": None,
                "progress": {"downloaded": 0, "size": size,
                             "percent": 0.0, "speed": 0, "eta": None},
                "added_at": time.time(), "finished_at": None,
            }
            self.jobs[jid] = job
            self.order.append(jid)
        self._save()
        self._wake.set()
        return job, True

    # nome antigo
    add_upload = add_transfer

    def add_install(self, filename):
        """Enfileira instalacao direta: o console baixa do PC e instala.

        Dispensa a copia em /data/pkg -- um jogo de 44 GB deixa de exigir 88 GB
        livres no console.
        """
        path = settings.dest / Path(filename).name
        if not path.exists():
            raise FileNotFoundError(filename)
        size = path.stat().st_size
        with self._lock:
            for j in self.jobs.values():
                if (j["type"] == "install" and j["filename"] == path.name
                        and j["status"] in ACTIVE):
                    return j, False
            jid = uuid.uuid4().hex[:12]
            job = {
                "id": jid, "type": "install", "url": None,
                "name": path.name, "filename": path.name, "size": size,
                "remote_dir": None, "dest_name": "instalação direta",
                "title_id": "", "region": "", "kind": "install", "cover_url": "",
                "status": "queued", "error": None, "result": None,
                "task_id": None, "console_title": "",
                "progress": {"downloaded": 0, "size": size,
                             "percent": 0.0, "speed": 0, "eta": None},
                "added_at": time.time(), "finished_at": None,
            }
            self.jobs[jid] = job
            self.order.append(jid)
        self._save()
        self._wake.set()
        return job, True

    def cancel(self, jid):
        with self._lock:
            job = self.jobs.get(jid)
            if not job:
                return False
            dl, ev = self._live.get(jid), self._cancels.get(jid)
            if job["status"] == "queued":
                job["status"] = "cancelled"
        if dl:
            dl.cancel()
        if ev:
            ev.set()
        self._save()
        return True

    def retry(self, jid):
        with self._lock:
            job = self.jobs.get(jid)
            if not job or job["status"] in ACTIVE:
                return False
            job["status"] = "queued"
            job["error"] = None
            job["finished_at"] = None
        self._save()
        self._wake.set()
        return True

    def remove(self, jid, delete_partial=False):
        self.cancel(jid)
        with self._lock:
            job = self.jobs.pop(jid, None)
            if jid in self.order:
                self.order.remove(jid)
        if job and delete_partial and job["type"] == "download":
            for p in (settings.incomplete / (job["filename"] + ".part"),
                      settings.incomplete / (job["filename"] + ".json")):
                Path(p).unlink(missing_ok=True)
        self._save()
        return bool(job)

    def clear_finished(self):
        with self._lock:
            gone = [i for i in self.order
                    if self.jobs[i]["status"] in ("done", "cancelled", "error")]
            for i in gone:
                self.jobs.pop(i, None)
                self.order.remove(i)
        self._save()
        return len(gone)

    def move(self, jid, delta):
        """Reordena a fila (so afeta jobs ainda nao iniciados)."""
        with self._lock:
            if jid not in self.order:
                return False
            i = self.order.index(jid)
            j = max(0, min(len(self.order) - 1, i + delta))
            self.order.insert(j, self.order.pop(i))
        self._save()
        return True

    def snapshot(self):
        with self._lock:
            out = []
            for jid in self.order:
                job = dict(self.jobs[jid])
                dl = self._live.get(jid)
                if dl is not None:
                    job["progress"] = dl.progress()
                out.append(job)
        return out

    # ---------- execucao ----------

    def _next(self):
        with self._lock:
            running = sum(1 for j in self.jobs.values() if j["status"] in ("downloading", "transferring", "installing"))
            if running >= int(settings["parallel_jobs"]):
                return None
            for jid in self.order:
                if self.jobs[jid]["status"] == "queued":
                    return jid
        return None

    def _loop(self):
        while True:
            jid = self._next()
            if jid is None:
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            threading.Thread(target=self._run, args=(jid,), daemon=True).start()
            time.sleep(0.3)

    def _run(self, jid):
        with self._lock:
            job = self.jobs.get(jid)
            if not job or job["status"] != "queued":
                return
            job["status"] = {"download": "downloading",
                             "install": "installing"}.get(job["type"], "transferring")
        self._save()
        try:
            if job["type"] == "download":
                self._run_download(job)
            elif job["type"] == "install":
                self._run_install(job)
            else:
                self._run_transfer(job)
        except Cancelled:
            job["status"] = "cancelled"
        except InterruptedError:
            job["status"] = "cancelled"
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            self._live.pop(jid, None)
            self._cancels.pop(jid, None)
            job["finished_at"] = time.time()
            self._save()
            self._wake.set()

    def _run_download(self, job):
        dl = Download(
            job["url"], settings.dest, settings.incomplete,
            filename=job["filename"],
            connections=int(job.get("connections") or settings["connections"]),
            chunk_mb=int(settings["chunk_mb"]),
            expected_size=job["size"],
            user_agent=job.get("user_agent"),
        )
        self._live[job["id"]] = dl
        last = [0.0]

        def tick(p):
            job["progress"] = p
            if time.time() - last[0] > 2:
                last[0] = time.time()
                self._save()

        dl.run(on_tick=tick)
        job["progress"] = dl.progress()
        job["size"] = dl.size
        job["status"] = "done"

    def _run_install(self, job):
        """O console puxa o arquivo deste PC e instala sozinho."""
        from .config import lan_ip
        from .server import pkg_alias

        ev = threading.Event()
        self._cancels[job["id"]] = ev

        alias = pkg_alias(job["filename"])
        url = f"http://{lan_ip()}:{settings['http_port']}/pkg/{alias}"
        job["url"] = url

        started = ps4.rpi_install([url])
        job["task_id"] = started["task_id"]
        job["console_title"] = started["title"]
        if started["title"]:
            job["name"] = started["title"]
        self._save()

        # Uma instalacao de 44 GB leva mais de uma hora, e nesse tempo o Package
        # Installer pode sair de foco no console -- a porta continua aceitando
        # TCP mas para de responder. Perder contato NAO e sinal de conclusao:
        # so os bytes transferidos dizem isso.
        MAX_SEM_CONTATO = 240          # ~12 min de falhas seguidas
        last_save = 0.0
        fails = 0
        last = None
        while True:
            if ev.is_set():
                try:
                    ps4._rpi_post("/api/stop_task", {"task_id": job["task_id"]})
                except Exception:
                    pass
                raise InterruptedError("instalação cancelada")
            time.sleep(3)

            try:
                pr = ps4.rpi_progress(job["task_id"])
            except ps4.RpiTaskGone:
                # o console respondeu que nao tem mais a tarefa
                if last and last["size"] and last["transferred"] >= last["size"]:
                    break
                got = (last or {}).get("transferred", 0)
                tot = (last or {}).get("size", 0) or job["size"]
                raise IOError(
                    "o console encerrou a tarefa antes de concluir "
                    f"({got / 1e9:.1f} de {tot / 1e9:.1f} GB). "
                    "Reinicie a instalação — ela recomeça do zero."
                )
            except Exception as e:
                # nao deu para falar com o instalador: insistir, nunca concluir
                fails += 1
                job["error"] = (f"sem contato com o instalador há {fails * 3}s "
                                f"({type(e).__name__}) — o console pode estar "
                                "continuando sozinho")
                self._save()
                if fails >= MAX_SEM_CONTATO:
                    raise IOError(
                        "perdi contato com o instalador remoto por mais de 12 min. "
                        "Verifique no console: o download pode ter continuado."
                    )
                continue

            fails = 0
            job["error"] = None
            last = pr
            if pr["error"]:
                raise IOError(f"o console reportou erro {pr['error']:#x} na instalação")
            job["progress"] = {
                "downloaded": pr["transferred"], "size": pr["size"] or job["size"],
                "percent": pr["percent"], "speed": 0,
                "eta": pr["rest_sec"] or None,
                "preparing": pr["preparing"], "installing": pr["installing"],
            }
            if time.time() - last_save > 2:
                last_save = time.time()
                self._save()
            if pr["done"]:
                break

        job["result"] = {"task_id": job["task_id"], "title": job["console_title"],
                         "url": url,
                         "transferred": (last or {}).get("transferred", 0)}
        ps4.invalidate_installed_cache()
        job["status"] = "done"

    def _run_transfer(self, job):
        ev = threading.Event()
        self._cancels[job["id"]] = ev
        last = [0.0]

        def tick(p):
            sp = p["speed"]
            left = p["size"] - p["sent"]
            job["progress"] = {
                "downloaded": p["sent"], "size": p["size"], "percent": p["percent"],
                "speed": sp, "eta": int(left / sp) if sp > 1024 and left > 0 else None,
                "resumed_from": p.get("resumed_from", 0),
            }
            if time.time() - last[0] > 2:
                last[0] = time.time()
                self._save()

        local = settings.dest / job["filename"]
        res = ps4.transfer(local, job.get("remote_dir"), on_progress=tick, cancel=ev)
        job["result"] = res
        job["progress"]["percent"] = 100.0
        job["progress"]["downloaded"] = res["size"]

        # so apaga a copia local depois que o console confirmou o tamanho
        if job.get("delete_after") and not res.get("skipped"):
            try:
                local.unlink()
                job["result"]["deleted_local"] = True
            except Exception as e:
                job["error"] = f"transferido, mas nao consegui apagar a copia local: {e}"
        job["status"] = "done"


manager = Manager()
