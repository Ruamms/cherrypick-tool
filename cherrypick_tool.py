"""Ferramenta de cherry-pick + push guiado.

Fluxo:
    fetch origin -> cria <branch final> a partir de origin/<branch base>
    -> cherry-pick dos commits informados -> mostra resumo -> push sob confirmacao
    -> abre no navegador a pagina de criacao do PR contra a branch base.

Nunca resolve conflito sozinho, nunca faz --continue as cegas e nunca empurra
sem clique explicito no botao Push. Abrir o PR e so a pagina de criacao: o PR
so nasce quando voce confirmar no navegador.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import webbrowser
from urllib.parse import quote

GIT = shutil.which("git") or "git"

STATE_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "cherrypick-tool")
STATE_FILE = os.path.join(STATE_DIR, "last.json")


class StepError(Exception):
    """Erro de validacao/execucao que deve parar o fluxo com mensagem propria."""


# ---------------------------------------------------------------- git helpers

def run_git(repo, args, log, quiet=False):
    """Executa git no repo e devolve (returncode, saida combinada)."""
    if not quiet:
        log("> git " + " ".join(args))
    proc = subprocess.run(
        [GIT, "-C", repo] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if out and not quiet:
        for line in out.splitlines():
            log("  " + line)
    return proc.returncode, out


def git_ok(repo, args):
    code, out = run_git(repo, args, lambda _m: None, quiet=True)
    return code == 0, out


def strip_origin(base):
    return base[len("origin/"):] if base.startswith("origin/") else base


def web_url_from_remote(url):
    """Converte a url do remote em url web (https). Devolve '' se nao reconhecer."""
    url = (url or "").strip()
    m = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url)          # git@github.com:Org/repo.git
    if not m:
        m = re.match(r"^\w+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)  # https:// | ssh://
    if not m:
        return ""
    host, path = m.group(1), m.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        return ""
    return "https://%s/%s" % (host, path)


def remote_web_url(repo):
    ok, url = git_ok(repo, ["remote", "get-url", "origin"])
    return web_url_from_remote(url) if ok else ""


def pr_compare_url(web, base, final):
    """Pagina de criacao de PR de <final> para <base> (GitHub)."""
    return "%s/compare/%s...%s?expand=1" % (
        web,
        quote(strip_origin(base), safe="/"),
        quote(final, safe="/"),
    )


# ---------------------------------------------------------------- validacoes

def parse_commits(text):
    """Aceita commits separados por linha, espaco, virgula ou ponto-e-virgula."""
    raw = text.replace(",", " ").replace(";", " ").split()
    return [c.strip() for c in raw if c.strip()]


def ensure_repo(repo, log):
    if not repo:
        raise StepError("Informe o caminho do repositorio.")
    if not os.path.isdir(repo):
        raise StepError("Caminho nao existe: %s" % repo)
    ok, out = git_ok(repo, ["rev-parse", "--is-inside-work-tree"])
    if not ok or out.strip() != "true":
        raise StepError("Nao e um repositorio git: %s" % repo)
    ok, top = git_ok(repo, ["rev-parse", "--show-toplevel"])
    if ok and top:
        log("Repositorio: %s" % top)
    ok, branch = git_ok(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if ok and branch:
        log("Branch atual: %s" % branch)
    return branch.strip() if ok else ""


def ensure_no_operation_in_progress(repo):
    if git_ok(repo, ["rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"])[0]:
        raise StepError(
            "Existe um cherry-pick em andamento neste repositorio. "
            "Finalize com 'git cherry-pick --continue' ou desfaca com '--abort' antes de comecar."
        )
    if git_ok(repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"])[0]:
        raise StepError("Existe um merge em andamento neste repositorio. Finalize antes de comecar.")


def ensure_clean(repo, log):
    ok, out = git_ok(repo, ["status", "--porcelain"])
    if not ok:
        raise StepError("Nao foi possivel ler o status do repositorio.")
    if out:
        log("Alteracoes pendentes:")
        for line in out.splitlines()[:20]:
            log("  " + line)
        raise StepError(
            "Working tree sujo. Commite, guarde em stash ou descarte as alteracoes antes de continuar."
        )


def ensure_valid_branch_name(repo, name):
    if not name:
        raise StepError("Informe o nome da branch final.")
    if not git_ok(repo, ["check-ref-format", "--branch", name])[0]:
        raise StepError("Nome de branch invalido: %s" % name)


def ensure_branch_absent(repo, final):
    if git_ok(repo, ["rev-parse", "-q", "--verify", "refs/heads/" + final])[0]:
        raise StepError(
            "A branch '%s' ja existe localmente (pode ser sobra de worktree). "
            "Apague ou use outro nome antes de continuar." % final
        )
    ok, out = git_ok(repo, ["ls-remote", "--heads", "origin", final])
    if ok and out:
        raise StepError("A branch '%s' ja existe no origin. Use outro nome." % final)


def resolve_base(repo, base):
    if not base:
        raise StepError("Informe a branch base.")
    ref = "origin/" + base if not base.startswith("origin/") else base
    if not git_ok(repo, ["rev-parse", "-q", "--verify", ref + "^{commit}"])[0]:
        raise StepError(
            "Base '%s' nao encontrada no origin depois do fetch. Confira o nome da branch base." % ref
        )
    return ref


def ensure_commits_exist(repo, commits):
    if not commits:
        raise StepError("Informe pelo menos um id de commit.")
    for sha in commits:
        ok, kind = git_ok(repo, ["cat-file", "-t", sha])
        if not ok or kind.strip() != "commit":
            raise StepError(
                "Commit '%s' nao encontrado neste repositorio. "
                "Confira o id ou traga a branch de origem (git fetch --all)." % sha
            )


# ---------------------------------------------------------------- fluxo

def prepare(repo, base, commits_text, final, log):
    """fetch + branch + cherry-pick. Devolve dict de resultado ou levanta StepError."""
    commits = parse_commits(commits_text)
    original_branch = ensure_repo(repo, log)
    ensure_valid_branch_name(repo, final)
    ensure_no_operation_in_progress(repo)
    ensure_clean(repo, log)

    log("")
    log("--- fetch ---")
    code, _ = run_git(repo, ["fetch", "origin", "--prune"], log)
    if code != 0:
        raise StepError("git fetch falhou. Veja a saida acima.")

    base_ref = resolve_base(repo, base)
    ensure_branch_absent(repo, final)
    ensure_commits_exist(repo, commits)

    log("")
    log("--- criando branch %s a partir de %s ---" % (final, base_ref))
    # --no-track: nao herda origin/<base> como upstream (evita push acidental na base).
    code, _ = run_git(repo, ["checkout", "--no-track", "-b", final, base_ref], log)
    if code != 0:
        raise StepError("Nao foi possivel criar a branch. Nada foi alterado.")

    log("")
    log("--- cherry-pick (%d commit(s)) ---" % len(commits))
    for i, sha in enumerate(commits, 1):
        _, subject = git_ok(repo, ["log", "-1", "--format=%h %s", sha])
        log("[%d/%d] %s" % (i, len(commits), subject))
        code, _ = run_git(repo, ["cherry-pick", sha], log)
        if code != 0:
            _, conflicts = git_ok(repo, ["diff", "--name-only", "--diff-filter=U"])
            log("")
            if conflicts:
                log("Arquivos em conflito:")
                for line in conflicts.splitlines():
                    log("  " + line)
            log("")
            log("O cherry-pick parou. Resolva na mao no repositorio e entao:")
            log("  git -C %s cherry-pick --continue" % repo)
            log("Depois volte aqui e use o botao Push (ele revalida o estado).")
            log("Para desfazer: botao 'Abortar cherry-pick' e depois")
            log("  git -C %s checkout %s && git -C %s branch -D %s"
                % (repo, original_branch or base, repo, final))
            raise StepError(
                "Conflito no cherry-pick de %s. Nada foi enviado; resolva no repositorio." % sha
            )

    log("")
    log("--- resumo ---")
    run_git(repo, ["log", "--oneline", "%s..HEAD" % base_ref], log)
    run_git(repo, ["diff", "--stat", "%s..HEAD" % base_ref], log)
    log("")
    log("Cherry-pick concluido sem conflito. Nada foi enviado ainda.")
    log("Confira o resumo e use o botao Push.")
    return {"repo": repo, "final": final, "base_ref": base_ref, "commits": commits}


def push(repo, final, base, log, abrir_pr=True):
    ensure_repo(repo, log)
    ensure_no_operation_in_progress(repo)
    ok, head = git_ok(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not ok or head.strip() != final:
        raise StepError(
            "O repositorio nao esta na branch '%s' (esta em '%s'). Push cancelado." % (final, head.strip())
        )
    ensure_clean(repo, log)
    log("")
    log("--- push ---")
    code, _ = run_git(repo, ["push", "-u", "origin", final], log)
    if code != 0:
        raise StepError("git push falhou. Veja a saida acima.")
    log("")
    log("Branch '%s' publicada no origin." % final)

    if not abrir_pr:
        log("Abertura do PR desmarcada. Nenhum PR foi aberto.")
        return
    web = remote_web_url(repo)
    if not web:
        log("Nao foi possivel identificar a url web do origin. Abra o PR na mao.")
        return
    if not base:
        log("Branch base em branco. Abra o PR na mao.")
        return
    url = pr_compare_url(web, base, final)
    log("PR (%s <- %s):" % (strip_origin(base), final))
    log("  " + url)
    try:
        webbrowser.open(url, new=2)
        log("Pagina de criacao do PR aberta no navegador. O PR so e criado quando voce confirmar la.")
    except Exception as exc:
        log("Nao foi possivel abrir o navegador (%r). Use a url acima." % (exc,))


def abort_cherry_pick(repo, log):
    ensure_repo(repo, log)
    if not git_ok(repo, ["rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"])[0]:
        raise StepError("Nao ha cherry-pick em andamento para abortar.")
    code, _ = run_git(repo, ["cherry-pick", "--abort"], log)
    if code != 0:
        raise StepError("git cherry-pick --abort falhou. Veja a saida acima.")
    log("Cherry-pick abortado. A branch criada continua existindo (apague na mao se quiser).")


# ---------------------------------------------------------------- estado

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- GUI

def main():
    import tkinter as tk
    from tkinter import filedialog, scrolledtext

    root = tk.Tk()
    root.title("Cherry-pick + Push")
    root.geometry("880x620")
    root.minsize(760, 520)

    state = load_state()
    log_queue = queue.Queue()
    prepared = {"ok": False}

    def log(msg=""):
        log_queue.put(msg)

    frm = tk.Frame(root, padx=10, pady=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    tk.Label(frm, text="Repositorio").grid(row=0, column=0, sticky="w", pady=3)
    repo_var = tk.StringVar(value=state.get("repo", ""))
    tk.Entry(frm, textvariable=repo_var).grid(row=0, column=1, sticky="ew", pady=3)

    def browse():
        path = filedialog.askdirectory(initialdir=repo_var.get() or os.path.expanduser("~"))
        if path:
            repo_var.set(path)

    tk.Button(frm, text="...", width=3, command=browse).grid(row=0, column=2, padx=(6, 0))

    tk.Label(frm, text="Branch base").grid(row=1, column=0, sticky="w", pady=3)
    base_var = tk.StringVar(value=state.get("base", ""))
    tk.Entry(frm, textvariable=base_var).grid(row=1, column=1, sticky="ew", pady=3)

    tk.Label(frm, text="Commits").grid(row=2, column=0, sticky="nw", pady=3)
    commits_txt = tk.Text(frm, height=4, wrap="none")
    commits_txt.grid(row=2, column=1, sticky="ew", pady=3)
    commits_txt.insert("1.0", state.get("commits", ""))
    tk.Label(frm, text="(um por linha,\nna ordem de aplicacao)", justify="left", fg="#666").grid(
        row=2, column=2, sticky="nw", padx=(6, 0)
    )

    tk.Label(frm, text="Branch final").grid(row=3, column=0, sticky="w", pady=3)
    final_var = tk.StringVar(value=state.get("final", ""))
    tk.Entry(frm, textvariable=final_var).grid(row=3, column=1, sticky="ew", pady=3)

    pr_var = tk.BooleanVar(value=bool(state.get("abrir_pr", True)))
    tk.Checkbutton(
        frm, text="Abrir a pagina do PR no navegador apos o push", variable=pr_var
    ).grid(row=4, column=1, sticky="w", pady=(6, 0))

    btns = tk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 6))
    btn_prep = tk.Button(btns, text="Preparar (fetch + branch + cherry-pick)", width=36)
    btn_prep.pack(side="left")
    btn_push = tk.Button(btns, text="Push", width=12, state="disabled")
    btn_push.pack(side="left", padx=6)
    btn_abort = tk.Button(btns, text="Abortar cherry-pick", width=20)
    btn_abort.pack(side="left", padx=6)

    out = scrolledtext.ScrolledText(frm, height=20, wrap="none", state="disabled")
    out.grid(row=6, column=0, columnspan=3, sticky="nsew")
    frm.rowconfigure(6, weight=1)

    status = tk.Label(frm, text="Pronto.", anchor="w", fg="#333")
    status.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(6, 0))

    def set_busy(busy):
        for b in (btn_prep, btn_abort):
            b.config(state="disabled" if busy else "normal")
        if busy:
            btn_push.config(state="disabled")
        elif prepared["ok"]:
            btn_push.config(state="normal")

    def pump():
        while True:
            try:
                msg = log_queue.get_nowait()
            except queue.Empty:
                break
            out.config(state="normal")
            out.insert("end", msg + "\n")
            out.see("end")
            out.config(state="disabled")
        root.after(80, pump)

    def in_thread(fn, done_msg):
        set_busy(True)
        status.config(text="Executando...", fg="#333")

        def worker():
            try:
                fn()
                root.after(0, lambda: status.config(text=done_msg, fg="#0a0"))
            except StepError as exc:
                prepared["ok"] = False
                log("")
                log("ERRO: %s" % exc)
                root.after(0, lambda: status.config(text=str(exc), fg="#c00"))
            except Exception as exc:  # falha inesperada: mostra e nao mascara
                prepared["ok"] = False
                log("")
                log("ERRO inesperado: %r" % (exc,))
                root.after(0, lambda: status.config(text="Erro inesperado: %r" % (exc,), fg="#c00"))
            finally:
                root.after(0, lambda: set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def clear_log():
        out.config(state="normal")
        out.delete("1.0", "end")
        out.config(state="disabled")

    def do_prepare():
        clear_log()
        prepared["ok"] = False
        repo = repo_var.get().strip()
        final = final_var.get().strip()
        save_state({
            "repo": repo,
            "base": base_var.get().strip(),
            "commits": commits_txt.get("1.0", "end").strip(),
            "final": final,
            "abrir_pr": bool(pr_var.get()),
        })

        def work():
            prepare(repo, base_var.get().strip(), commits_txt.get("1.0", "end"), final, log)
            prepared["ok"] = True

        in_thread(work, "Cherry-pick pronto. Revise e clique em Push.")

    def do_push():
        in_thread(
            lambda: push(
                repo_var.get().strip(),
                final_var.get().strip(),
                base_var.get().strip(),
                log,
                bool(pr_var.get()),
            ),
            "Push concluido.",
        )

    def do_abort():
        in_thread(lambda: abort_cherry_pick(repo_var.get().strip(), log), "Cherry-pick abortado.")

    btn_prep.config(command=do_prepare)
    btn_push.config(command=do_push)
    btn_abort.config(command=do_abort)

    log("Preencha os campos e clique em Preparar.")
    log("Nada e enviado ao origin sem o botao Push.")
    pump()
    root.mainloop()


if __name__ == "__main__":
    main()
