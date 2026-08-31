"""Ferramenta de cherry-pick + push guiado.

Fluxo:
    fetch origin -> cria <branch final> a partir de origin/<branch base>
    -> cherry-pick dos commits informados -> mostra resumo -> push sob confirmação
    -> abre no navegador a página de criação do PR contra a branch base.

Nunca resolve conflito sozinho, nunca faz --continue às cegas e nunca empurra
sem clique explícito no botão Push. Abrir o PR é só a página de criação: o PR
só nasce quando você confirmar no navegador.
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
    """Erro de validação/execução que deve parar o fluxo com mensagem própria."""


# ---------------------------------------------------------------- git helpers

def run_git(repo, args, log, quiet=False):
    """Executa git no repo e devolve (returncode, saída combinada)."""
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
    """Converte a url do remote em url web (https). Devolve '' se não reconhecer."""
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
    """Página de criação de PR de <final> para <base> (GitHub)."""
    return "%s/compare/%s...%s?expand=1" % (
        web,
        quote(strip_origin(base), safe="/"),
        quote(final, safe="/"),
    )


# ---------------------------------------------------------------- validações

def parse_commits(text):
    """Aceita commits separados por linha, espaço, virgula ou ponto-e-virgula."""
    raw = text.replace(",", " ").replace(";", " ").split()
    return [c.strip() for c in raw if c.strip()]


def ensure_repo(repo, log):
    if not repo:
        raise StepError("Informe o caminho do repositório.")
    if not os.path.isdir(repo):
        raise StepError("Caminho não existe: %s" % repo)
    ok, out = git_ok(repo, ["rev-parse", "--is-inside-work-tree"])
    if not ok or out.strip() != "true":
        raise StepError("Não é um repositório git: %s" % repo)
    ok, top = git_ok(repo, ["rev-parse", "--show-toplevel"])
    if ok and top:
        log("Repositório: %s" % top)
    ok, branch = git_ok(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if ok and branch:
        log("Branch atual: %s" % branch)
    return branch.strip() if ok else ""


def ensure_no_operation_in_progress(repo):
    if git_ok(repo, ["rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"])[0]:
        raise StepError(
            "Existe um cherry-pick em andamento neste repositório. "
            "Finalize com 'git cherry-pick --continue' ou desfaça com '--abort' antes de começar."
        )
    if git_ok(repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"])[0]:
        raise StepError("Existe um merge em andamento neste repositório. Finalize antes de começar.")


def ensure_clean(repo, log):
    ok, out = git_ok(repo, ["status", "--porcelain"])
    if not ok:
        raise StepError("Não foi possível ler o status do repositório.")
    if out:
        log("Alterações pendentes:")
        for line in out.splitlines()[:20]:
            log("  " + line)
        raise StepError(
            "Working tree sujo. Commite, guarde em stash ou descarte as alterações antes de continuar."
        )


def ensure_valid_branch_name(repo, name):
    if not name:
        raise StepError("Informe o nome da branch final.")
    if not git_ok(repo, ["check-ref-format", "--branch", name])[0]:
        raise StepError("Nome de branch inválido: %s" % name)


def ensure_branch_absent(repo, final):
    if git_ok(repo, ["rev-parse", "-q", "--verify", "refs/heads/" + final])[0]:
        raise StepError(
            "A branch '%s' já existe localmente (pode ser sobra de worktree). "
            "Apague ou use outro nome antes de continuar." % final
        )
    ok, out = git_ok(repo, ["ls-remote", "--heads", "origin", final])
    if ok and out:
        raise StepError("A branch '%s' já existe no origin. Use outro nome." % final)


def resolve_base(repo, base):
    if not base:
        raise StepError("Informe a branch base.")
    ref = "origin/" + base if not base.startswith("origin/") else base
    if not git_ok(repo, ["rev-parse", "-q", "--verify", ref + "^{commit}"])[0]:
        raise StepError(
            "Base '%s' não encontrada no origin depois do fetch. Confira o nome da branch base." % ref
        )
    return ref


def ensure_commits_exist(repo, commits):
    if not commits:
        raise StepError("Informe pelo menos um id de commit.")
    for sha in commits:
        ok, kind = git_ok(repo, ["cat-file", "-t", sha])
        if not ok or kind.strip() != "commit":
            raise StepError(
                "Commit '%s' não encontrado neste repositório. "
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
        raise StepError("git fetch falhou. Veja a saída acima.")

    base_ref = resolve_base(repo, base)
    ensure_branch_absent(repo, final)
    ensure_commits_exist(repo, commits)

    log("")
    log("--- criando branch %s a partir de %s ---" % (final, base_ref))
    # --no-track: não herda origin/<base> como upstream (evita push acidental na base).
    code, _ = run_git(repo, ["checkout", "--no-track", "-b", final, base_ref], log)
    if code != 0:
        raise StepError("Não foi possível criar a branch. Nada foi alterado.")

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
            log("O cherry-pick parou. Resolva na mão no repositório e então:")
            log("  git -C %s cherry-pick --continue" % repo)
            log("Depois volte aqui e use o botão Push (ele revalida o estado).")
            log("Para desfazer: botão 'Abortar cherry-pick' e depois")
            log("  git -C %s checkout %s && git -C %s branch -D %s"
                % (repo, original_branch or base, repo, final))
            raise StepError(
                "Conflito no cherry-pick de %s. Nada foi enviado; resolva no repositório." % sha
            )

    log("")
    log("--- resumo ---")
    run_git(repo, ["log", "--oneline", "%s..HEAD" % base_ref], log)
    run_git(repo, ["diff", "--stat", "%s..HEAD" % base_ref], log)
    log("")
    log("Cherry-pick concluído sem conflito. Nada foi enviado ainda.")
    log("Confira o resumo e use o botão Push.")
    return {"repo": repo, "final": final, "base_ref": base_ref, "commits": commits}


def push(repo, final, base, log, abrir_pr=True):
    ensure_repo(repo, log)
    ensure_no_operation_in_progress(repo)
    ok, head = git_ok(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not ok or head.strip() != final:
        raise StepError(
            "O repositório não está na branch '%s' (está em '%s'). Push cancelado." % (final, head.strip())
        )
    ensure_clean(repo, log)
    log("")
    log("--- push ---")
    code, _ = run_git(repo, ["push", "-u", "origin", final], log)
    if code != 0:
        raise StepError("git push falhou. Veja a saída acima.")
    log("")
    log("Branch '%s' publicada no origin." % final)

    if not abrir_pr:
        log("Abertura do PR desmarcada. Nenhum PR foi aberto.")
        return
    web = remote_web_url(repo)
    if not web:
        log("Não foi possível identificar a url web do origin. Abra o PR na mão.")
        return
    if not base:
        log("Branch base em branco. Abra o PR na mão.")
        return
    url = pr_compare_url(web, base, final)
    log("PR (%s <- %s):" % (strip_origin(base), final))
    log("  " + url)
    try:
        webbrowser.open(url, new=2)
        log("Página de criação do PR aberta no navegador. O PR só é criado quando você confirmar lá.")
    except Exception as exc:
        log("Não foi possível abrir o navegador (%r). Use a url acima." % (exc,))


def abort_cherry_pick(repo, log):
    ensure_repo(repo, log)
    if not git_ok(repo, ["rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"])[0]:
        raise StepError("Não há cherry-pick em andamento para abortar.")
    code, _ = run_git(repo, ["cherry-pick", "--abort"], log)
    if code != 0:
        raise StepError("git cherry-pick --abort falhou. Veja a saída acima.")
    log("Cherry-pick abortado. A branch criada continua existindo (apague na mão se quiser).")


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


# ---------------------------------------------------------------- tema

# Uma cor por papel, num lugar so: as duas janelas leem daqui, e trocar de tema
# e trocar estes valores.
TEMA = {
    "fundo": "#1e1f22",
    "fundo_campo": "#2b2d31",
    "fundo_alt": "#26282c",
    "texto": "#dcdde1",
    "texto_fraco": "#a8adb5",
    "dica": "#7f858d",
    "erro": "#ff6b6b",
    "ok": "#5ec269",
    "destaque": "#6fa8ff",
    "borda": "#3a3d42",
    "selecao": "#3d5a80",
}


def aplicar_tema(root):
    """Pinta a janela de escuro. Devolve o dicionário de cores.

    Tem de rodar ANTES de criar os widgets: o tk clássico lê o banco de opções
    no momento em que cada widget nasce, e o que já existe não é repintado.
    """
    import tkinter as tk
    from tkinter import ttk

    t = TEMA
    root.configure(bg=t["fundo"])
    opcoes = (
        ("*background", t["fundo"]),
        ("*foreground", t["texto"]),
        ("*highlightBackground", t["fundo"]),
        ("*highlightColor", t["borda"]),
        ("*Button.background", t["fundo_campo"]),
        ("*Button.activeBackground", t["selecao"]),
        ("*Button.activeForeground", t["texto"]),
        ("*Button.disabledForeground", t["dica"]),
        ("*Entry.background", t["fundo_campo"]),
        ("*Entry.foreground", t["texto"]),
        ("*Entry.insertBackground", t["texto"]),
        ("*Entry.readonlyBackground", t["fundo_alt"]),
        ("*Entry.disabledBackground", t["fundo_alt"]),
        ("*Entry.disabledForeground", t["dica"]),
        ("*Entry.selectBackground", t["selecao"]),
        ("*Checkbutton.activeBackground", t["fundo"]),
        ("*Checkbutton.activeForeground", t["texto"]),
        ("*Checkbutton.selectColor", t["fundo_campo"]),
        ("*Text.background", t["fundo_campo"]),
        ("*Text.foreground", t["texto"]),
        ("*Text.insertBackground", t["texto"]),
        ("*Text.selectBackground", t["selecao"]),
        ("*Scrollbar.background", t["fundo_alt"]),
        ("*Scrollbar.troughColor", t["fundo"]),
        ("*Scrollbar.activeBackground", t["selecao"]),
        # a lista que abre no combobox e um Listbox tk, fora do ttk
        ("*TCombobox*Listbox.background", t["fundo_campo"]),
        ("*TCombobox*Listbox.foreground", t["texto"]),
        ("*TCombobox*Listbox.selectBackground", t["selecao"]),
        ("*TCombobox*Listbox.selectForeground", t["texto"]),
    )
    for padrao, valor in opcoes:
        root.option_add(padrao, valor)

    estilo = ttk.Style(root)
    # o tema nativo do Windows desenha por bitmap e ignora cor de fundo; o clam
    # aceita. Sem esta linha a grade continua branca.
    try:
        estilo.theme_use("clam")
    except tk.TclError:
        pass
    estilo.configure(".", background=t["fundo"], foreground=t["texto"],
                     fieldbackground=t["fundo_campo"], bordercolor=t["borda"],
                     lightcolor=t["fundo_alt"], darkcolor=t["fundo_alt"],
                     troughcolor=t["fundo"], focuscolor=t["destaque"])
    estilo.configure("TNotebook", background=t["fundo"], borderwidth=0)
    estilo.configure("TNotebook.Tab", background=t["fundo_alt"],
                     foreground=t["texto_fraco"], padding=(14, 6), borderwidth=0)
    estilo.map("TNotebook.Tab",
               background=[("selected", t["fundo_campo"])],
               foreground=[("selected", t["texto"])])
    estilo.configure("Treeview", background=t["fundo_campo"], foreground=t["texto"],
                     fieldbackground=t["fundo_campo"], borderwidth=0, rowheight=20)
    estilo.configure("Treeview.Heading", background=t["fundo_alt"], foreground=t["texto"],
                     relief="flat", borderwidth=1)
    estilo.map("Treeview.Heading", background=[("active", t["selecao"])])
    estilo.map("Treeview", background=[("selected", t["selecao"])],
               foreground=[("selected", "#ffffff")])
    estilo.configure("TCombobox", fieldbackground=t["fundo_campo"],
                     background=t["fundo_campo"], foreground=t["texto"],
                     arrowcolor=t["texto"], selectbackground=t["fundo_campo"],
                     selectforeground=t["texto"])
    estilo.map("TCombobox",
               fieldbackground=[("readonly", t["fundo_campo"])],
               foreground=[("readonly", t["texto"])],
               arrowcolor=[("disabled", t["dica"])])
    estilo.configure("TScrollbar", background=t["fundo_alt"], troughcolor=t["fundo"],
                     arrowcolor=t["texto_fraco"], bordercolor=t["borda"])
    estilo.configure("TFrame", background=t["fundo"])
    estilo.configure("TLabel", background=t["fundo"], foreground=t["texto"])
    return t


def criar_balao(widget, resolver, tema, atraso=350):
    """Balão de ajuda que aparece ao parar o mouse sobre o widget.

    resolver(evento) -> o texto daquele ponto, ou '' para não mostrar nada. É o
    que permite tirar a legenda da tela: a explicação passa a morar na célula,
    em vez de um bloco fixo ocupando um pedaço da janela.
    """
    import tkinter as tk

    estado = {"janela": None, "tarefa": None, "texto": ""}

    def esconder(_evento=None):
        if estado["tarefa"]:
            widget.after_cancel(estado["tarefa"])
            estado["tarefa"] = None
        if estado["janela"]:
            estado["janela"].destroy()
            estado["janela"] = None
        estado["texto"] = ""

    def mostrar(texto, x, y):
        if estado["janela"]:
            estado["janela"].destroy()
        janela = tk.Toplevel(widget)
        janela.overrideredirect(True)      # sem borda e fora da ordem de janelas
        janela.attributes("-topmost", True)
        tk.Label(janela, text=texto, justify="left", background=tema["fundo_alt"],
                 foreground=tema["texto"], relief="solid", borderwidth=1,
                 wraplength=560, padx=8, pady=5).pack()
        janela.geometry("+%d+%d" % (x + 16, y + 20))
        estado["janela"] = janela
        estado["texto"] = texto

    def mover(evento):
        texto = resolver(evento) or ""
        if texto and texto == estado["texto"]:
            return                        # mesma célula: deixa o balão quieto
        esconder()
        if not texto:
            return
        x, y = evento.x_root, evento.y_root
        estado["tarefa"] = widget.after(atraso, lambda: mostrar(texto, x, y))

    widget.bind("<Motion>", mover, add="+")
    widget.bind("<Leave>", esconder, add="+")
    widget.bind("<Button-1>", esconder, add="+")
    return esconder


# ---------------------------------------------------------------- GUI

def main():
    import tkinter as tk
    from tkinter import filedialog, scrolledtext

    root = tk.Tk()
    root.title("Cherry-pick + Push")
    root.geometry("880x620")
    root.minsize(760, 520)
    tema = aplicar_tema(root)

    state = load_state()
    log_queue = queue.Queue()
    prepared = {"ok": False}

    def log(msg=""):
        log_queue.put(msg)

    frm = tk.Frame(root, padx=10, pady=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    tk.Label(frm, text="Repositório").grid(row=0, column=0, sticky="w", pady=3)
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
    tk.Label(frm, text="(um por linha,\nna ordem de aplicação)", justify="left", fg=tema["dica"]).grid(
        row=2, column=2, sticky="nw", padx=(6, 0)
    )

    tk.Label(frm, text="Branch final").grid(row=3, column=0, sticky="w", pady=3)
    final_var = tk.StringVar(value=state.get("final", ""))
    tk.Entry(frm, textvariable=final_var).grid(row=3, column=1, sticky="ew", pady=3)

    pr_var = tk.BooleanVar(value=bool(state.get("abrir_pr", True)))
    tk.Checkbutton(
        frm, text="Abrir a página do PR no navegador após o push", variable=pr_var
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

    status = tk.Label(frm, text="Pronto.", anchor="w", fg=tema["texto"])
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
        status.config(text="Executando...", fg=tema["texto"])

        def worker():
            try:
                fn()
                root.after(0, lambda: status.config(text=done_msg, fg=tema["ok"]))
            except StepError as exc:
                prepared["ok"] = False
                log("")
                log("ERRO: %s" % exc)
                root.after(0, lambda: status.config(text=str(exc), fg=tema["erro"]))
            except Exception as exc:  # falha inesperada: mostra e não mascara
                prepared["ok"] = False
                log("")
                log("ERRO inesperado: %r" % (exc,))
                root.after(0, lambda: status.config(text="Erro inesperado: %r" % (exc,), fg=tema["erro"]))
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
            "Push concluído.",
        )

    def do_abort():
        in_thread(lambda: abort_cherry_pick(repo_var.get().strip(), log), "Cherry-pick abortado.")

    btn_prep.config(command=do_prepare)
    btn_push.config(command=do_push)
    btn_abort.config(command=do_abort)

    log("Preencha os campos e clique em Preparar.")
    log("Nada é enviado ao origin sem o botão Push.")
    pump()
    root.mainloop()


if __name__ == "__main__":
    main()
