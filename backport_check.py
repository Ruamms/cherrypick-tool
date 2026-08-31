"""BackportCheck: o que está na master e ainda não chegou na branch de produção.

Compara `origin/<principal>` com `origin/<produção>` e lista o que falta portar,
classificando cada commit em quatro situações:

    PENDENTE  - nenhum sinal de que foi portado
    BRANCH    - já existe branch de backport no origin, mas nada na produção
                (backport publicado e PR não mergeado)
    PROVÁVEL  - o número da tarefa aparece em algum commit da produção, com outro
                assunto (típico de PR de backport intitulado com o nome da branch)
    (portado) - patch-id equivalente ou assunto igual: não entra na lista

O botão de backport reaproveita o fluxo do CherryPickPush: cria a branch a partir
de origin/<produção>, faz cherry-pick, push e abre a página do PR. Em conflito ele
para e não empurra nada.
"""

import json
import os
import queue
import re
import threading
import unicodedata

from cherrypick_tool import (
    STATE_DIR,
    StepError,
    ensure_repo,
    git_ok,
    prepare,
    push,
    run_git,
    strip_origin,
)

STATE_FILE = os.path.join(STATE_DIR, "backport.json")

# Janela de histórico lida na produção para decidir o que já foi portado.
# Independe do filtro de dias da master: um backport pode ter sido feito bem depois.
JANELA_PRODUCAO_DIAS = 1095

PENDENTE = "PENDENTE"
BRANCH = "BRANCH CRIADA"
PROVAVEL = "PROVÁVEL"
PORTADO = "PORTADO"

ORDEM = {PENDENTE: 0, BRANCH: 1, PROVAVEL: 2}

CORES = {PENDENTE: "#ff6b6b", BRANCH: "#ffa657", PROVAVEL: "#9aa0a6"}

LEGENDA = (
    (PENDENTE, "nenhum sinal de backport - é o que quebra cliente"),
    (BRANCH, "a branch de backport existe no origin, mas nada chegou na produção (PR não mergeado)"),
    (PROVAVEL, "a OP já aparece na produção com outro assunto - confira antes de descartar"),
)

TODOS = "(todos)"
TODAS = "(todas)"

# coluna conflito
NAO_CHECADO = "..."
LIMPO = "limpo"
CONFLITO = "conflito"
SEM_INFO = "?"


# ---------------------------------------------------------------- normalização

def norm_assunto(texto):
    """Assunto comparável: sem (#1234), sem acento, só alfanumérico."""
    texto = re.sub(r"\(#\d+\)", " ", texto)
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texto).split())


def ops_do_assunto(texto):
    """Números de OP do assunto. Tira antes o (#1234) do PR para não confundir."""
    return set(re.findall(r"\b\d{5,7}\b", re.sub(r"\(#\d+\)", " ", texto)))


def pr_do_assunto(texto):
    achados = re.findall(r"\(#(\d+)\)", texto)
    return achados[-1] if achados else ""


def sufixo_producao(producao):
    """release-2601 -> 2601 ; v2.2701 -> 2701 ; fallback: nome sem pontuação."""
    nome = strip_origin(producao)
    numeros = re.findall(r"\d+", nome)
    return numeros[-1] if numeros else re.sub(r"[^A-Za-z0-9]+", "", nome)


def nome_branch_padrao(op, sha, sufixo):
    return "fb_%s_%s" % (op or sha[:7], sufixo)


# ---------------------------------------------------------------- análise

def _ref_valida(repo, ref):
    return git_ok(repo, ["rev-parse", "-q", "--verify", ref + "^{commit}"])[0]


def indice_producao(repo, ref_prod):
    """Assuntos normalizados e números de tarefa já presentes na produção."""
    ok, saida = git_ok(repo, [
        "log", "--format=%s", ref_prod, "--since=%d days ago" % JANELA_PRODUCAO_DIAS,
    ])
    if not ok:
        raise StepError("Não foi possível ler o histórico de %s." % ref_prod)
    assuntos, ops = set(), {}
    for linha in saida.splitlines():
        chave = norm_assunto(linha)
        if chave:
            assuntos.add(chave)
        for op in ops_do_assunto(linha):
            ops.setdefault(op, linha.strip())
    return assuntos, ops, len(saida.splitlines())


def branches_do_origin(repo):
    ok, saida = git_ok(repo, ["ls-remote", "--heads", "origin"])
    if not ok:
        return []
    nomes = []
    for linha in saida.splitlines():
        partes = linha.split("refs/heads/", 1)
        if len(partes) == 2:
            nomes.append(partes[1].strip())
    return nomes


def classificar(assunto, assuntos_prod, ops_prod, branches_remotas, sufixo):
    """Situação de um commit da principal em relação à produção."""
    if norm_assunto(assunto) in assuntos_prod:
        return PORTADO, "assunto idêntico já está na produção"
    ops = ops_do_assunto(assunto)
    comuns = sorted(ops & set(ops_prod))
    if comuns:
        return PROVAVEL, "tarefa %s já aparece na produção: %s" % (comuns[0], ops_prod[comuns[0]])
    achadas = [b for b in branches_remotas if sufixo in b and any(op in b for op in ops)]
    if achadas:
        return BRANCH, "branch no origin sem merge na produção: %s" % ", ".join(achadas[:3])
    return PENDENTE, ""


def commit_do_pr(repo, ref_main, numero):
    """Acha na principal o commit do squash merge do PR <número>. ('', '') se não houver."""
    ok, saida = git_ok(repo, [
        "log", ref_main, "--fixed-strings", "--grep=(#%s)" % numero,
        "--format=%H%x1f%s%x1f%ad", "--date=short", "-2",
    ])
    if not ok or not saida.strip():
        return "", "", ""
    primeiro = saida.splitlines()[0]
    if primeiro.count("\x1f") < 2:
        return "", "", ""
    sha, assunto, data = primeiro.split("\x1f", 2)
    return sha, assunto, data


def resolver_repos(texto, repo_local, log):
    """Aceita 'org/repo' ou o caminho de um clone; devolve (lista, {repo: caminho}).

    Caminho de pasta é resolvido pelo remote origin daquele clone - foi o engano
    mais fácil de cometer, e é também o que faz o git escolher a conta certa.
    """
    from github_prs import repo_do_remote

    itens = [p.strip() for p in (texto or "").replace(";", ",").split(",") if p.strip()]
    if not itens and repo_local:
        itens = [repo_local]
    resolvidos, locais = [], {}
    for item in itens:
        if os.path.isdir(item):
            ok, url = git_ok(item, ["remote", "get-url", "origin"])
            nome = repo_do_remote(url) if ok else ""
            if not nome:
                raise StepError("Não consegui descobrir o org/repo do clone em %s." % item)
            log("  %s -> %s" % (item, nome))
        elif "/" in item and ":" not in item and "\\" not in item:
            nome = item.strip("/")
        else:
            raise StepError(
                "'%s' não é 'org/repo' nem uma pasta de clone existente." % item)
        if nome not in resolvidos:
            resolvidos.append(nome)
            if os.path.isdir(item):
                locais[nome] = item
    if not resolvidos:
        raise StepError("Informe ao menos um repositório (org/repo ou a pasta de um clone).")
    return resolvidos, locais


def analisar(repo, producao, principal, dias, log):
    """Devolve (linhas, portados_por_autor, autores).

    Não filtra por autor: quem filtra é a tela, para trocar de autor sem
    reprocessar o git. Levanta StepError em erro de uso.
    """
    ensure_repo(repo, log)
    if not producao:
        raise StepError("Informe a branch de produção.")
    if not principal:
        raise StepError("Informe a branch principal.")

    log("")
    log("--- fetch ---")
    if run_git(repo, ["fetch", "origin", "--prune"], log)[0] != 0:
        raise StepError("git fetch falhou. Veja a saída acima.")

    ref_prod = "origin/" + strip_origin(producao)
    ref_main = "origin/" + strip_origin(principal)
    for ref in (ref_prod, ref_main):
        if not _ref_valida(repo, ref):
            raise StepError("Branch '%s' não encontrada no origin depois do fetch." % ref)

    sufixo = sufixo_producao(producao)
    log("")
    log("--- lendo %s (últimos %d dias) ---" % (ref_prod, JANELA_PRODUCAO_DIAS))
    assuntos_prod = set()
    ops_prod = {}
    ok, saida = git_ok(repo, [
        "log", "--format=%s", ref_prod, "--since=%d days ago" % JANELA_PRODUCAO_DIAS,
    ])
    if not ok:
        raise StepError("Não foi possível ler o histórico de %s." % ref_prod)
    for linha in saida.splitlines():
        chave = norm_assunto(linha)
        if chave:
            assuntos_prod.add(chave)
        for op in ops_do_assunto(linha):
            ops_prod.setdefault(op, linha.strip())
    log("%d commits lidos na produção." % len(saida.splitlines()))

    log("")
    log("--- branches de backport no origin ---")
    ok, saida = git_ok(repo, ["ls-remote", "--heads", "origin"])
    branches_remotas = []
    if ok:
        for linha in saida.splitlines():
            partes = linha.split("refs/heads/", 1)
            if len(partes) == 2:
                branches_remotas.append(partes[1].strip())
    log("%d branches remotas." % len(branches_remotas))

    log("")
    log("--- comparando %s com %s (últimos %s dias) ---" % (ref_main, ref_prod, dias))
    # --cherry-pick --right-only: só o que existe na master e não tem equivalente
    # (mesmo patch-id) na produção. Pega o cherry-pick limpo; o que veio com
    # conflito resolvido diferente sobra e cai nas regras de assunto/OP abaixo.
    ok, saida = git_ok(repo, [
        "log", "--no-merges", "--cherry-pick", "--right-only",
        "--since=%s days ago" % dias, "--date=short",
        "--format=%H%x1f%an%x1f%ad%x1f%s",
        "%s...%s" % (ref_prod, ref_main),
    ])
    if not ok:
        raise StepError("Não foi possível comparar as branches. Confira os nomes.")

    linhas = []
    portados = {}
    autores = set()
    for bruto in saida.splitlines():
        if bruto.count("\x1f") < 3:
            continue
        sha, nome_autor, data, assunto = bruto.split("\x1f", 3)
        autores.add(nome_autor)
        if norm_assunto(assunto) in assuntos_prod:
            portados[nome_autor] = portados.get(nome_autor, 0) + 1
            continue

        ops = ops_do_assunto(assunto)
        comuns = sorted(ops & set(ops_prod))
        if comuns:
            status = PROVAVEL
            detalhe = "OP %s já aparece na produção: %s" % (comuns[0], ops_prod[comuns[0]])
        else:
            achadas = [
                b for b in branches_remotas
                if sufixo in b and any(op in b for op in ops)
            ]
            if achadas:
                status = BRANCH
                detalhe = "branch no origin sem merge na produção: %s" % ", ".join(achadas[:3])
            else:
                status = PENDENTE
                detalhe = ""

        linhas.append({
            "status": status,
            "data": data,
            "autor": nome_autor,
            "op": sorted(ops)[0] if ops else "",
            "pr": pr_do_assunto(assunto),
            "assunto": assunto,
            "sha": sha,
            "detalhe": detalhe,
            "conflito": NAO_CHECADO,
            "arquivos": [],
        })

    # status mais urgente primeiro; dentro do status, mais recente primeiro
    linhas.sort(key=lambda x: (ORDEM.get(x["status"], 9), _inverso(x["data"])))
    return linhas, portados, sorted(autores)


def _inverso(data):
    """Chave para ordenar data decrescente dentro do mesmo status."""
    return tuple(-int(p) for p in data.split("-")) if re.match(r"^\d+-\d+-\d+$", data) else (0,)


# ---------------------------------------------------------------- conflito

def checar_conflito(repo, producao, sha):
    """Simula o cherry-pick de <sha> sobre a produção SEM tocar no working tree.

    `git merge-tree` faz o merge de 3 vias só em memória: base = <sha>^,
    lados = ponta da produção e <sha>. Sai 0 limpo, 1 com conflito.
    Devolve (estado, arquivos_em_conflito).
    """
    if not git_ok(repo, ["rev-parse", "-q", "--verify", sha + "^{commit}"])[0]:
        return SEM_INFO, []
    if not git_ok(repo, ["rev-parse", "-q", "--verify", sha + "^^{commit}"])[0]:
        return SEM_INFO, []  # commit raiz: não há base para o merge de 3 vias
    ref = "origin/" + strip_origin(producao)
    codigo, saida = run_git(repo, [
        "merge-tree", "--write-tree", "--name-only", "--merge-base", sha + "^", ref, sha,
    ], lambda _m: None, quiet=True)
    if codigo == 0:
        return LIMPO, []
    if codigo != 1:
        return SEM_INFO, []
    # linha 1 = oid da árvore; depois, os arquivos até a primeira linha em branco
    arquivos = []
    for linha in saida.splitlines()[1:]:
        if not linha.strip():
            break
        arquivos.append(linha.strip())
    return CONFLITO, arquivos


def texto_conflito(linha):
    if linha["conflito"] == CONFLITO:
        return "conflito (%d arq.)" % len(linha["arquivos"])
    return linha["conflito"]


# ---------------------------------------------------------------- backport

def backportar(repo, producao, commits, final, log):
    """cherry-pick + push + PR, reaproveitando o fluxo do CherryPickPush."""
    prepare(repo, strip_origin(producao), " ".join(commits), final, log)
    push(repo, final, strip_origin(producao), log, True)


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
    import webbrowser
    from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

    from cherrypick_tool import (
        aplicar_tema, criar_balao, pr_compare_url, remote_web_url,
    )

    root = tk.Tk()
    root.title("BackportCheck - o que falta na produção")
    root.geometry("1300x960")
    root.minsize(1000, 760)
    tema = aplicar_tema(root)

    state = load_state()
    log_queue = queue.Queue()
    dados = []                                   # linhas visíveis, na ordem da grade
    cache = {"linhas": [], "portados": {}}       # resultado completo da última análise
    checagem = {"gen": 0}                        # geração da checagem de conflito em curso

    def log(msg=""):
        log_queue.put(msg)

    def dica(entry, var, texto):
        """Exemplo em cinza dentro do campo vazio.

        É um rótulo posicionado sobre o Entry, não um texto colocado na variável:
        assim o valor lido nunca pode ser o exemplo por acidente.
        """
        rotulo = tk.Label(entry, text=texto, fg=tema["dica"], anchor="w",
                          bg=entry.cget("background"))
        rotulo.bind("<Button-1>", lambda _e: entry.focus_set())

        def atualizar(*_):
            if var.get():
                rotulo.place_forget()
            else:
                rotulo.place(x=4, rely=0.5, anchor="w")

        var.trace_add("write", atualizar)
        entry.bind("<FocusIn>", lambda _e: rotulo.place_forget())
        entry.bind("<FocusOut>", lambda _e: atualizar())
        atualizar()

    abas = ttk.Notebook(root)
    abas.pack(fill="both", expand=True, padx=8, pady=(8, 0))
    aba_git = tk.Frame(abas)
    abas.add(aba_git, text="  Backport (git)  ")
    aba_ciclo = tk.Frame(abas)
    abas.add(aba_ciclo, text="  Ciclo (PRs abertos)  ")

    def ligar_ordenacao(grid, colunas, titulos, ordem, chaves, redesenhar):
        """Clique no cabeçalho ordena; clique de novo inverte."""
        def clicar(coluna):
            if coluna not in chaves:
                return
            if ordem["col"] == coluna:
                ordem["desc"] = not ordem["desc"]
            else:
                ordem["col"], ordem["desc"] = coluna, False
            for col in colunas:
                texto = titulos.get(col, col.upper())
                if col == ordem["col"]:
                    texto += "  ▼" if ordem["desc"] else "  ▲"
                grid.heading(col, text=texto)
            redesenhar()

        for col in colunas:
            grid.heading(col, command=lambda c=col: clicar(c))

    def aplicar_ordem(linhas, ordem, chaves):
        if ordem["col"] in chaves:
            linhas.sort(key=chaves[ordem["col"]], reverse=ordem["desc"])
        return linhas

    topo = tk.Frame(aba_git, padx=10, pady=8)
    topo.pack(fill="x")
    topo.columnconfigure(1, weight=1)

    tk.Label(topo, text="Repositório").grid(row=0, column=0, sticky="w", pady=2)
    repo_var = tk.StringVar(value=state.get("repo", ""))
    campo_repo = tk.Entry(topo, textvariable=repo_var)
    campo_repo.grid(row=0, column=1, columnspan=3, sticky="ew", pady=2)
    dica(campo_repo, repo_var, "C:/repos/meu-projeto")

    def browse():
        caminho = filedialog.askdirectory(initialdir=repo_var.get() or os.path.expanduser("~"))
        if caminho:
            repo_var.set(caminho)

    tk.Button(topo, text="...", width=3, command=browse).grid(row=0, column=4, padx=(6, 0))

    tk.Label(topo, text="Branch de produção").grid(row=1, column=0, sticky="w", pady=2)
    prod_var = tk.StringVar(value=state.get("producao", ""))
    campo_prod = tk.Entry(topo, textvariable=prod_var, width=18)
    campo_prod.grid(row=1, column=1, sticky="w", pady=2)
    dica(campo_prod, prod_var, "release-2601")

    tk.Label(topo, text="Branch principal").grid(row=1, column=2, sticky="e", padx=(12, 6))
    main_var = tk.StringVar(value=state.get("principal", "master"))
    tk.Entry(topo, textvariable=main_var, width=18).grid(row=1, column=3, sticky="w")

    tk.Label(topo, text="Autor").grid(row=2, column=0, sticky="w", pady=2)
    autor_var = tk.StringVar(value=state.get("autor", "") or TODOS)
    autor_combo = ttk.Combobox(topo, textvariable=autor_var, width=26, state="readonly")
    autor_combo["values"] = [TODOS] + ([state["autor"]] if state.get("autor") else [])
    autor_combo.grid(row=2, column=1, sticky="w", pady=2)
    tk.Label(topo, text="(preenchido pela análise)", fg=tema["texto_fraco"]).grid(
        row=2, column=2, sticky="e", padx=(12, 6)
    )

    tk.Label(topo, text="Dias").grid(row=2, column=3, sticky="w", padx=(120, 0))
    dias_var = tk.StringVar(value=str(state.get("dias", "180")))
    tk.Entry(topo, textvariable=dias_var, width=6).grid(row=2, column=3, sticky="e")

    btns = tk.Frame(aba_git, padx=10)
    btns.pack(fill="x")
    btn_analisar = tk.Button(btns, text="Analisar", width=14)
    btn_analisar.pack(side="left")
    btn_backport = tk.Button(btns, text="Cherry-pick + push do selecionado", width=32, state="disabled")
    btn_backport.pack(side="left", padx=6)
    btn_pr = tk.Button(btns, text="Abrir PR de origem", width=18, state="disabled")
    btn_pr.pack(side="left")
    tk.Label(
        btns,
        text="(selecione várias linhas para levar tudo na mesma branch)",
        fg=tema["texto_fraco"],
    ).pack(side="left", padx=10)

    legenda = tk.Frame(aba_git, padx=10, pady=4)
    legenda.pack(fill="x")
    tk.Label(legenda, fg=tema["dica"], text=(
        "Pare o mouse numa célula (ou no cabeçalho) para ver o que ela diz."
    )).pack(side="left")

    corpo = tk.Frame(aba_git, padx=10, pady=8)
    corpo.pack(fill="both", expand=True)

    colunas = ("status", "conflito", "data", "autor", "op", "pr", "assunto")
    larguras = (110, 100, 82, 130, 70, 60, 540)
    grid = ttk.Treeview(corpo, columns=colunas, show="headings", selectmode="extended", height=15)
    for col, larg in zip(colunas, larguras):
        grid.heading(col, text=col.upper())
        grid.column(col, width=larg, anchor="w", stretch=(col == "assunto"))
    barra = ttk.Scrollbar(corpo, orient="vertical", command=grid.yview)
    grid.configure(yscrollcommand=barra.set)
    grid.pack(side="left", fill="both", expand=True)
    barra.pack(side="left", fill="y")

    for situacao, cor in CORES.items():
        grid.tag_configure(situacao, foreground=cor)

    AJUDA_GIT = {
        "status": "Situação do commit em relação à produção. Pare o mouse na célula.\n"
                  "O que já foi portado (patch-id equivalente ou assunto igual) não entra "
                  "na lista.",
        "conflito": "Cherry-pick simulado sobre a produção, em memória: nada é alterado no "
                    "seu repo.",
        "data": "Data do commit na branch principal.",
        "autor": "Quem fez o commit.",
        "op": "Número da tarefa achado no assunto do commit.",
        "pr": "Número do PR que trouxe o commit (do '(#1234)' do squash merge).",
        "assunto": "Assunto do commit. Duplo clique abre o PR de origem.",
    }
    COLUNAS_GIT = {"#%d" % (i + 1): nome for i, nome in enumerate(colunas)}
    EXPLICACAO_CONFLITO = {
        LIMPO: "aplica sem conflito - dá para portar agora",
        NAO_CHECADO: "ainda checando",
        SEM_INFO: "não deu para simular (commit raiz, ou git anterior ao 2.38)",
        CONFLITO: "vai parar para você resolver na mão",
    }

    def balao_git(evento):
        regiao = grid.identify_region(evento.x, evento.y)
        coluna = COLUNAS_GIT.get(grid.identify_column(evento.x))
        if not coluna:
            return ""
        if regiao == "heading":
            return AJUDA_GIT.get(coluna, "")
        if regiao != "cell":
            return ""
        item = grid.identify_row(evento.y)
        try:
            linha = dados[int(item)]
        except (ValueError, IndexError, TypeError):
            return ""
        if coluna == "status":
            texto = dict(LEGENDA).get(linha["status"], "")
            return "%s: %s%s" % (linha["status"], texto,
                                 "\n" + linha["detalhe"] if linha["detalhe"] else "")
        if coluna == "conflito":
            explicacao = EXPLICACAO_CONFLITO.get(linha["conflito"], "")
            if linha["conflito"] == CONFLITO and linha["arquivos"]:
                return "%s\n%s" % (explicacao, "\n".join("  " + a for a in linha["arquivos"][:12]))
            return explicacao
        if coluna == "assunto":
            return linha["assunto"]
        return AJUDA_GIT.get(coluna, "")

    criar_balao(grid, balao_git, tema)

    out = scrolledtext.ScrolledText(root, height=9, wrap="none", state="disabled")
    out.pack(fill="both", expand=False, padx=10)

    status = tk.Label(root, text="Preencha e clique em Analisar.", anchor="w", fg=tema["texto"], padx=10)
    status.pack(fill="x", pady=(4, 8))

    def set_busy(busy):
        btn_analisar.config(state="disabled" if busy else "normal")
        estado = "disabled" if (busy or not grid.selection()) else "normal"
        btn_backport.config(state=estado)
        btn_pr.config(state=estado)

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

    def in_thread(fn, msg_ok, depois=None):
        set_busy(True)
        status.config(text="Executando...", fg=tema["texto"])

        def worker():
            try:
                fn()
                root.after(0, lambda: status.config(text=msg_ok, fg=tema["ok"]))
                if depois:
                    root.after(0, depois)
            except StepError as exc:
                log("")
                log("ERRO: %s" % exc)
                root.after(0, lambda: status.config(text=str(exc), fg=tema["erro"]))
            except Exception as exc:
                log("")
                log("ERRO inesperado: %r" % (exc,))
                root.after(0, lambda: status.config(text="Erro inesperado: %r" % (exc,), fg=tema["erro"]))
            finally:
                root.after(0, lambda: set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def selecionadas():
        return [dados[int(i)] for i in grid.selection()]

    def on_select(_evt=None):
        estado = "normal" if grid.selection() else "disabled"
        btn_backport.config(state=estado)
        btn_pr.config(state=estado)
        marcadas = selecionadas()
        if len(marcadas) != 1:
            return
        linha = marcadas[0]
        if linha["conflito"] == CONFLITO:
            status.config(
                text="Conflita em: %s" % ", ".join(linha["arquivos"][:6]), fg=CORES[PENDENTE]
            )
        elif linha["detalhe"]:
            status.config(text=linha["detalhe"], fg=tema["texto"])

    grid.bind("<<TreeviewSelect>>", on_select)

    def salvar():
        """Grava o estado das duas abas. O token vai cifrado pela DPAPI; nunca em claro."""
        from openproject import proteger as _proteger
        state.update({
            "repo": repo_var.get().strip(),
            "producao": prod_var.get().strip(),
            "principal": main_var.get().strip(),
            "autor": "" if autor_var.get() == TODOS else autor_var.get(),
            "dias": dias_var.get().strip() or "180",
            "repos": repos_var.get().strip(),
            "op_url": op_url_var.get().strip(),
            "query": query_var.get().strip(),
            "token_cifrado": _proteger(token_var.get().strip()),
            "autor_ciclo": "" if autor_c_var.get() == TODOS else autor_c_var.get(),
            "dias_parado": dias_parado_var.get().strip() or "7",
            "conta": conta_var.get(),
            "producao_ciclo": prod_c_var.get().strip(),
            "homologacao": homo_c_var.get().strip(),
            "entrega_filtro": entrega_var.get(),
            "versao_filtro": versao_var.get(),
            "dias_tarefas": dias_tarefas_var.get().strip() or "180",
        })
        save_state(state)

    ordem_git = {"col": None, "desc": False}
    CHAVES_GIT = {
        "status": lambda l: (ORDEM.get(l["status"], 9), l["data"]),
        "conflito": lambda l: {CONFLITO: 0, NAO_CHECADO: 1, SEM_INFO: 2, LIMPO: 3}.get(
            l["conflito"], 9),
        "data": lambda l: l["data"],
        "autor": lambda l: l["autor"].lower(),
        "op": lambda l: int(l["op"]) if l["op"].isdigit() else -1,
        "pr": lambda l: int(l["pr"]) if l["pr"].isdigit() else -1,
        "assunto": lambda l: l["assunto"].lower(),
    }

    def render():
        """Redesenha a grade a partir do cache, aplicando o filtro de autor."""
        autor = autor_var.get()
        if autor == TODOS:
            linhas = list(cache["linhas"])
            portados = sum(cache["portados"].values())
        else:
            linhas = [l for l in cache["linhas"] if l["autor"] == autor]
            portados = cache["portados"].get(autor, 0)
        aplicar_ordem(linhas, ordem_git, CHAVES_GIT)
        dados[:] = linhas
        grid.delete(*grid.get_children())
        for i, linha in enumerate(linhas):
            grid.insert("", "end", iid=str(i), tags=(linha["status"],), values=(
                linha["status"], texto_conflito(linha), linha["data"], linha["autor"][:18],
                linha["op"], linha["pr"], linha["assunto"],
            ))
        contagem = {}
        for linha in linhas:
            contagem[linha["status"]] = contagem.get(linha["status"], 0) + 1
        resumo = "%d pendente(s), %d com branch criada, %d provável(is); %d já portado(s) fora da lista." % (
            contagem.get(PENDENTE, 0), contagem.get(BRANCH, 0),
            contagem.get(PROVAVEL, 0), portados,
        )
        status.config(text=resumo, fg=tema["texto"])
        checar_conflitos()
        return resumo

    ligar_ordenacao(grid, colunas, {}, ordem_git, CHAVES_GIT, lambda: render())

    def checar_conflitos():
        """Preenche a coluna conflito em segundo plano, sem travar os botões."""
        checagem["gen"] += 1
        geracao = checagem["gen"]
        repo = repo_var.get().strip()
        producao = prod_var.get().strip()
        alvos = [(i, l) for i, l in enumerate(dados) if l["conflito"] == NAO_CHECADO]
        if not alvos:
            return

        def atualizar(indice, linha):
            if checagem["gen"] != geracao or not grid.exists(str(indice)):
                return
            grid.set(str(indice), "conflito", texto_conflito(linha))

        def worker():
            for indice, linha in alvos:
                if checagem["gen"] != geracao:
                    return
                estado, arquivos = checar_conflito(repo, producao, linha["sha"])
                linha["conflito"], linha["arquivos"] = estado, arquivos
                root.after(0, atualizar, indice, linha)
            root.after(0, lambda: log("Checagem de conflito concluída (%d commits)." % len(alvos)))

        threading.Thread(target=worker, daemon=True).start()

    def on_autor(_evt=None):
        salvar()
        render()

    autor_combo.bind("<<ComboboxSelected>>", on_autor)

    def concluir_analise():
        cache["linhas"] = resultado.get("linhas", [])
        cache["portados"] = resultado.get("portados", {})
        autores = resultado.get("autores", [])
        autor_combo["values"] = [TODOS] + autores
        if autor_var.get() not in [TODOS] + autores:
            autor_var.set(TODOS)
        log("")
        log(render())

    resultado = {}

    def do_analisar():
        out.config(state="normal")
        out.delete("1.0", "end")
        out.config(state="disabled")
        checagem["gen"] += 1  # cancela checagem de conflito da rodada anterior
        repo = repo_var.get().strip()
        producao = prod_var.get().strip()
        principal = main_var.get().strip()
        dias = (dias_var.get().strip() or "180")
        if not dias.isdigit():
            status.config(text="Dias deve ser um número.", fg=tema["erro"])
            return
        salvar()

        def work():
            (resultado["linhas"], resultado["portados"],
             resultado["autores"]) = analisar(repo, producao, principal, dias, log)

        in_thread(work, "Análise concluída.", depois=concluir_analise)

    def do_backport():
        marcadas = selecionadas()
        if not marcadas:
            return
        repo = repo_var.get().strip()
        producao = prod_var.get().strip()
        # ordem cronológica de aplicação
        marcadas.sort(key=lambda x: x["data"])
        sufixo = sufixo_producao(producao)
        op = next((m["op"] for m in marcadas if m["op"]), "")
        sugestao = nome_branch_padrao(op, marcadas[0]["sha"], sufixo)

        texto = "Vai criar a branch a partir de origin/%s e levar %d commit(s):\n\n%s\n\nNome da branch:" % (
            strip_origin(producao), len(marcadas),
            "\n".join(
                "  [%s] %s %s" % (texto_conflito(m), m["sha"][:9], m["assunto"][:64])
                for m in marcadas
            ),
        )
        final = simpledialog.askstring("Confirmar backport", texto, initialvalue=sugestao, parent=root)
        if not final:
            return
        final = final.strip()
        avisos = ["  %s - %s" % (m["status"], m["detalhe"] or m["assunto"][:60])
                  for m in marcadas if m["status"] != PENDENTE]
        conflitados = ["  %s conflita em: %s" % (m["sha"][:9], ", ".join(m["arquivos"][:4]))
                       for m in marcadas if m["conflito"] == CONFLITO]
        if (avisos or conflitados) and not messagebox.askokcancel(
            "Atenção",
            "%s%sContinuar mesmo assim?" % (
                ("Sinal de que talvez já tenham sido portados:\n%s\n\n" % "\n".join(avisos)) if avisos else "",
                ("O cherry-pick vai parar em conflito:\n%s\n\n" % "\n".join(conflitados)) if conflitados else "",
            ),
            parent=root,
        ):
            return

        log("")
        log("=== backport para %s ===" % final)
        in_thread(
            lambda: backportar(repo, producao, [m["sha"] for m in marcadas], final, log),
            "Backport publicado. Confira a página do PR no navegador.",
            depois=do_analisar,
        )

    def do_abrir_pr():
        marcadas = selecionadas()
        if not marcadas:
            return
        web = remote_web_url(repo_var.get().strip())
        if not web:
            status.config(text="Não foi possível identificar a url do origin.", fg=tema["erro"])
            return
        for m in marcadas[:5]:
            url = "%s/pull/%s" % (web, m["pr"]) if m["pr"] else "%s/commit/%s" % (web, m["sha"])
            log("abrindo %s" % url)
            webbrowser.open(url, new=2)

    def do_duplo(_evt=None):
        do_abrir_pr()

    grid.bind("<Double-1>", do_duplo)
    btn_analisar.config(command=do_analisar)
    btn_backport.config(command=do_backport)
    btn_pr.config(command=do_abrir_pr)

    # ------------------------------------------------------------- aba Ciclo
    import ciclo as mod_ciclo
    import excel
    import github_prs
    from openproject import (
        CAMPO_ENTREGA, CAMPO_RAMOS, PREFIXO_RAMOS, OpenProject, campo_por_nome,
        desproteger, separar_projeto, titulo_do_link,
    )

    ciclo_cache = {"linhas": [], "tipos": [], "status": [], "fechados": [], "usuario": ""}
    ciclo_dados = []
    resultado_ciclo = {}

    topo_c = tk.Frame(aba_ciclo, padx=10, pady=8)
    topo_c.pack(fill="x")
    topo_c.columnconfigure(1, weight=1)

    def rotulo(pai, texto, ajuda, **posicao):
        """Rótulo de campo com a explicação no balão, em vez de num parágrafo fixo."""
        alvo = tk.Label(pai, text=texto)
        alvo.grid(**posicao)
        if ajuda:
            criar_balao(alvo, lambda _evento, t=ajuda: t, tema)
        return alvo

    rotulo(topo_c, "Repositórios",
           "org/repo separados por vírgula, OU o caminho de um clone - nesse caso o org/repo\n"
           "sai do remote origin. É do clone que sai a resposta 'o commit já está na branch?'.",
           row=0, column=0, sticky="w", pady=2)
    repos_var = tk.StringVar(value=state.get("repos", ""))
    campo_repos = tk.Entry(topo_c, textvariable=repos_var)
    campo_repos.grid(row=0, column=1, columnspan=3, sticky="ew", pady=2)
    dica(campo_repos, repos_var, "minha-org/um-repo, minha-org/outro-repo")
    tk.Label(topo_c, text="org/repo OU a pasta de um clone", fg=tema["texto_fraco"]).grid(
        row=0, column=4, sticky="w", padx=(6, 0))

    rotulo(topo_c, "OpenProject",
           "URL da instância. Cole a URL de um PROJETO (.../projects/meu-time) para conferir\n"
           "TODAS as tarefas dele, e não só as que têm PR aberto - é assim que a tarefa cujo\n"
           "PR foi mergeado e apagado continua sendo cobrada nas outras branches.",
           row=1, column=0, sticky="w", pady=2)
    op_url_var = tk.StringVar(value=state.get("op_url", ""))
    campo_url = tk.Entry(topo_c, textvariable=op_url_var)
    campo_url.grid(row=1, column=1, sticky="ew", pady=2)
    dica(campo_url, op_url_var, "https://openproject.suaempresa.com.br/projects/meu-time")
    rotulo(topo_c, "Query salva (id)",
           "Opcional: o número que aparece na URL do OpenProject como ?query_id=1234, a visão\n"
           "já filtrada do seu time. Traz também tarefas sem PR aberto.",
           row=1, column=2, sticky="e", padx=(12, 6))
    query_var = tk.StringVar(value=state.get("query", ""))
    campo_query = tk.Entry(topo_c, textvariable=query_var, width=10)
    campo_query.grid(row=1, column=3, sticky="w")
    dica(campo_query, query_var, "1234")
    quadro_dias_t = tk.Frame(topo_c)
    quadro_dias_t.grid(row=1, column=4, sticky="w", padx=(6, 0))
    ajuda_dias_t = tk.Label(quadro_dias_t, text="Tarefas dos últimos")
    ajuda_dias_t.pack(side="left")
    criar_balao(ajuda_dias_t, lambda _e: (
        "Janela de tarefas do PROJETO: só entram as mexidas nesse período.\n"
        "Vale apenas quando a URL do OpenProject aponta para um projeto."), tema)
    dias_tarefas_var = tk.StringVar(value=str(state.get("dias_tarefas", "180")))
    tk.Entry(quadro_dias_t, textvariable=dias_tarefas_var, width=5).pack(side="left", padx=(6, 4))
    tk.Label(quadro_dias_t, text="dias").pack(side="left")

    rotulo(topo_c, "Token da API",
           "Token pessoal do OpenProject (Minha conta -> Tokens de acesso), NUNCA a sua senha.\n"
           "Fica cifrado nesta máquina (DPAPI) e volta preenchido na próxima vez.",
           row=2, column=0, sticky="w", pady=2)
    token_var = tk.StringVar(value=desproteger(state.get("token_cifrado", "")))
    campo_token = tk.Entry(topo_c, textvariable=token_var, show="")
    campo_token.grid(row=2, column=1, sticky="ew", pady=2)
    dica(campo_token, token_var, "cole aqui o token de API")

    def esconder_token(*_):
        campo_token.config(show="*" if token_var.get() else "")

    token_var.trace_add("write", esconder_token)
    esconder_token()

    def abrir_pagina_token():
        # a URL pode apontar para um projeto; a pagina de token e da instancia
        url, _projeto = separar_projeto(op_url_var.get())
        if not url:
            status.config(text="Preencha a URL do OpenProject primeiro.", fg=tema["erro"])
            return
        webbrowser.open(url + "/my/access_token", new=2)

    tk.Button(topo_c, text="Onde pegar o token", command=abrir_pagina_token).grid(
        row=2, column=2, columnspan=2, sticky="w", padx=(12, 0))

    tk.Label(topo_c, text="Autor").grid(row=3, column=0, sticky="w", pady=2)
    autor_c_var = tk.StringVar(value=state.get("autor_ciclo", "") or TODOS)
    autor_c_combo = ttk.Combobox(topo_c, textvariable=autor_c_var, width=26, state="readonly")
    autor_c_combo["values"] = [TODOS] + ([state["autor_ciclo"]] if state.get("autor_ciclo") else [])
    autor_c_combo.grid(row=3, column=1, sticky="w", pady=2)
    rotulo(topo_c, "Branch de produção",
           "A versão que está em produção hoje (ex.: v2.2601).",
           row=3, column=2, sticky="e", padx=(12, 6))
    prod_c_var = tk.StringVar(value=state.get("producao_ciclo", "") or state.get("producao", ""))
    campo_prod_c = tk.Entry(topo_c, textvariable=prod_c_var, width=16)
    campo_prod_c.grid(row=3, column=3, sticky="w")
    dica(campo_prod_c, prod_c_var, "release-2601")

    rotulo(topo_c, "Branch de homologação",
           "A próxima versão (ex.: v2.2602). Vazio: a ferramenta trabalha com uma branch de\n"
           "versão só, como antes.",
           row=4, column=0, sticky="w", pady=2)
    homo_c_var = tk.StringVar(value=state.get("homologacao", ""))
    campo_homo_c = tk.Entry(topo_c, textvariable=homo_c_var, width=26)
    campo_homo_c.grid(row=4, column=1, sticky="w", pady=2)
    dica(campo_homo_c, homo_c_var, "release-2602 (vazio = não usa)")

    rotulo(topo_c, "Parado após (dias)",
           "Dias sem NENHUMA atualização no PR para ele ser marcado como PARADO.",
           row=4, column=2, sticky="e", padx=(12, 6))
    dias_parado_var = tk.StringVar(value=str(state.get("dias_parado", "7")))
    tk.Entry(topo_c, textvariable=dias_parado_var, width=5).grid(row=4, column=3, sticky="w")
    rotulo(topo_c, "Conta do GitHub",
           "Qual credencial usar. 'automática pelo repositório' pergunta ao git usando o\n"
           "org/repo - é o que resolve máquina com mais de uma conta.",
           row=5, column=0, sticky="w", pady=2)
    contas = [github_prs.AUTOMATICA, github_prs.PADRAO_GIT] + [
        u for _alvo, u in github_prs.contas_guardadas()]
    conta_var = tk.StringVar(value=state.get("conta", github_prs.AUTOMATICA))
    conta_combo = ttk.Combobox(topo_c, textvariable=conta_var, width=26, state="readonly",
                               values=contas)
    conta_combo.grid(row=5, column=1, sticky="w", pady=2)
    if conta_var.get() not in contas:
        conta_var.set(github_prs.AUTOMATICA)
    rotulo_conta = tk.Label(topo_c, text="", fg=tema["ok"])
    rotulo_conta.grid(row=5, column=2, columnspan=3, sticky="w", padx=(12, 0))


    btns_c = tk.Frame(aba_ciclo, padx=10)
    btns_c.pack(fill="x")
    btn_carregar = tk.Button(btns_c, text="Carregar", width=14)
    btn_carregar.pack(side="left")
    btn_tipos = tk.Button(btns_c, text="Tipos que exigem produção...", width=28)
    btn_tipos.pack(side="left", padx=6)
    btn_status = tk.Button(btns_c, text="Status que liberam merge...", width=26)
    btn_status.pack(side="left")
    btn_abrir_wp = tk.Button(btns_c, text="Abrir tarefa / PR", width=16, state="disabled")
    btn_abrir_wp.pack(side="left", padx=6)
    btn_excel = tk.Button(btns_c, text="Exportar Excel...", width=16, state="disabled")
    btn_excel.pack(side="left")

    # filtros sobre o resultado já carregado: trocar filtro não refaz consulta
    filtros_c = tk.Frame(aba_ciclo, padx=10, pady=4)
    filtros_c.pack(fill="x")
    tk.Label(filtros_c, text="Filtrar:  entrega ao cliente").pack(side="left")
    entrega_var = tk.StringVar(value=state.get("entrega_filtro", TODOS))
    entrega_combo = ttk.Combobox(filtros_c, textvariable=entrega_var, width=10, state="readonly",
                                 values=[TODOS, "Sim", "Não"])
    entrega_combo.pack(side="left", padx=(6, 0))
    if entrega_var.get() not in (TODOS, "Sim", "Não"):
        entrega_var.set(TODOS)
    tk.Label(filtros_c, text="versão pedida (ramos)").pack(side="left", padx=(14, 0))
    versao_var = tk.StringVar(value=state.get("versao_filtro", TODAS))
    versao_combo = ttk.Combobox(filtros_c, textvariable=versao_var, width=12, state="readonly",
                                values=[TODAS])
    versao_combo.pack(side="left", padx=(6, 0))
    tk.Label(filtros_c, fg=tema["dica"], text=(
        "|   pare o mouse em cima de uma célula (ou do cabeçalho) para ver o que ela diz"
    )).pack(side="left", padx=(10, 0))

    corpo_c = tk.Frame(aba_ciclo, padx=10, pady=8)
    corpo_c.pack(fill="both", expand=True)
    colunas_c = ("pendencia", "pendente_em", "tarefa", "tipo", "status", "entrega", "ramos",
                 "principal", "producao", "homologacao", "outros", "build", "dias", "assunto")
    larguras_c = (150, 210, 65, 95, 105, 55, 85, 165, 165, 165, 105, 105, 40, 260)
    titulos_c = {"principal": "PR PRINCIPAL", "producao": "PR PRODUÇÃO",
                 "homologacao": "PR HOMOLOGAÇÃO", "outros": "PR OUTRAS BRANCHES",
                 "build": "BUILD (X5)", "dias": "DIAS", "pendencia": "PENDÊNCIA",
                 "pendente_em": "PENDENTE EM", "tarefa": "TAREFA", "tipo": "TIPO",
                 "status": "STATUS", "entrega": "ENTREGA?", "ramos": "RAMOS",
                 "assunto": "ASSUNTO"}
    grid_c = ttk.Treeview(corpo_c, columns=colunas_c, show="headings", selectmode="extended", height=13)
    for col, larg in zip(colunas_c, larguras_c):
        grid_c.heading(col, text=titulos_c.get(col, col.upper()))
        grid_c.column(col, width=larg, anchor="w", stretch=(col == "assunto"))
    barra_c = ttk.Scrollbar(corpo_c, orient="vertical", command=grid_c.yview)
    grid_c.configure(yscrollcommand=barra_c.set)
    grid_c.pack(side="left", fill="both", expand=True)
    barra_c.pack(side="left", fill="y")
    for situacao, cor in mod_ciclo.CORES_CICLO.items():
        grid_c.tag_configure(situacao, foreground=cor)

    def selecionadas_c():
        return [ciclo_dados[int(i)] for i in grid_c.selection()]

    def on_select_c(_evt=None):
        btn_abrir_wp.config(state="normal" if grid_c.selection() else "disabled")
        marcadas = selecionadas_c()
        if len(marcadas) == 1 and marcadas[0]["detalhe"]:
            status.config(text=marcadas[0]["detalhe"], fg=tema["texto"])

    grid_c.bind("<<TreeviewSelect>>", on_select_c)

    # número da coluna -> chave de urls; calculado, para não quebrar quando a
    # ordem das colunas muda
    COLUNAS_LINK = {"#%d" % (colunas_c.index(nome) + 1): nome
                    for nome in ("principal", "producao", "homologacao", "outros")}

    def _links_da_celula(evento):
        """URLs do PR sob o cursor, ou [] se a célula não for de PR."""
        if grid_c.identify_region(evento.x, evento.y) != "cell":
            return []
        lado = COLUNAS_LINK.get(grid_c.identify_column(evento.x))
        item = grid_c.identify_row(evento.y)
        if not lado or not item:
            return []
        try:
            linha = ciclo_dados[int(item)]
        except (ValueError, IndexError):
            return []
        return (linha.get("urls") or {}).get(lado, [])

    def clicar_celula(evento):
        for url in _links_da_celula(evento)[:4]:
            log("abrindo %s" % url)
            webbrowser.open(url, new=2)

    def mover_no_grid(evento):
        grid_c.config(cursor="hand2" if _links_da_celula(evento) else "")

    grid_c.bind("<Button-1>", clicar_celula, add="+")
    grid_c.bind("<Motion>", mover_no_grid)
    grid_c.bind("<Leave>", lambda _e: grid_c.config(cursor=""))

    COLUNAS_NUM = {"#%d" % (i + 1): nome for i, nome in enumerate(colunas_c)}
    # o que cada coluna quer dizer: aparece ao parar o mouse no cabeçalho
    AJUDA_COLUNA = {
        "pendencia": "O que fazer com essa tarefa. Pare o mouse na célula para a explicação.",
        "pendente_em": "Em qual branch a tarefa ainda falta, e como está cada uma.",
        "tarefa": "Número da tarefa no OpenProject. Duplo clique abre a tarefa e os PRs.",
        "tipo": "Tipo da tarefa. Vem do gerenciador; sem tarefa lá, é deduzido do título do PR.",
        "status": "Status da tarefa no gerenciador.",
        "entrega": "Campo 'Confirmar entrega ao cliente?' da tarefa.",
        "ramos": "Versões lidas do campo 'ramos para disponibilização' - a interpretação da "
                 "ferramenta, dá para conferir.",
        "principal": "Situação da branch principal. Clique para abrir o PR.",
        "producao": "Situação da branch de produção. Clique para abrir o PR.",
        "homologacao": "Situação da branch de homologação. Clique para abrir o PR.",
        "outros": "PRs abertos para branches que não são nenhuma das três. Clique para abrir.",
        "build": "Campo de build (X5) da tarefa.",
        "dias": "Dias desde a última atualização do PR mais recente da tarefa.",
        "assunto": "Assunto da tarefa no gerenciador, ou o título do PR quando não há tarefa.",
    }

    def texto_do_balao(evento):
        regiao = grid_c.identify_region(evento.x, evento.y)
        coluna = COLUNAS_NUM.get(grid_c.identify_column(evento.x))
        if not coluna:
            return ""
        if regiao == "heading":
            return AJUDA_COLUNA.get(coluna, "")
        if regiao != "cell":
            return ""
        item = grid_c.identify_row(evento.y)
        try:
            linha = ciclo_dados[int(item)]
        except (ValueError, IndexError, TypeError):
            return ""
        if coluna == "pendencia":
            partes = ["%s: %s" % (linha["pendencia"],
                                  mod_ciclo.explicar_pendencia(linha["pendencia"]))]
            if linha["detalhe"]:
                partes.append("Nesta tarefa: " + linha["detalhe"])
            return "\n".join(partes)
        if coluna == "pendente_em":
            if not linha["pendente_em"]:
                return "Nada pendente: a tarefa está em todas as branches obrigatórias."
            return ("Falta em:\n  %s\n\nObrigatórias: %s"
                    % ("\n  ".join(linha["pendente_em"].split(", ")),
                       ", ".join(linha["obrigatorias"]) or "-"))
        if coluna in ("principal", "producao", "homologacao"):
            situacao = linha["pr_%s" % coluna]
            if not situacao:
                return "Branch não configurada."
            explicacao = mod_ciclo.explicar_situacao(situacao)
            fim = "\n\nClique para abrir o PR." if (linha["urls"] or {}).get(coluna) else ""
            return "%s\n%s%s" % (situacao, explicacao, fim)
        if coluna == "entrega":
            if not linha.get("tem_entrega"):
                return ("O campo 'Confirmar entrega ao cliente?' não veio nesta tarefa - "
                        "confira o nome do campo no OpenProject.")
            return ("Confirmar entrega ao cliente? = %s\n%s"
                    % ("sim" if linha["entrega"] else "não",
                       "as versões do campo de ramos são obrigatórias" if linha["entrega"]
                       else "só a principal é obrigatória (e a produção, se o tipo exigir)"))
        if coluna == "ramos":
            if not linha["ramos"]:
                return "A tarefa não pediu nenhuma versão."
            return "No card: %s\nVersões lidas: %s" % (
                linha["ramos"], ", ".join(linha["versoes"]) or "nenhuma")
        if coluna == "tipo":
            return "%s\n(veio do %s)" % (linha["tipo"], linha["origem_tipo"])
        if coluna == "assunto":
            return linha["assunto"]
        if coluna == "outros" and linha["pr_outros"]:
            return linha["pr_outros"] + "\n\nClique para abrir."
        if coluna == "build":
            return linha["build"] or "Campo de build vazio."
        if coluna == "dias":
            return "%d dia(s) desde a última atualização do PR." % linha["idade"]
        if coluna == "tarefa":
            return "Duplo clique abre a tarefa no OpenProject e os PRs dela."
        return AJUDA_COLUNA.get(coluna, "")

    criar_balao(grid_c, texto_do_balao, tema)

    def _num_pr(texto):
        numeros = re.findall(r"\d+", texto or "")
        return int(numeros[0]) if numeros else -1

    def _entrega(linha):
        if not linha.get("tem_entrega"):
            return "-"
        return "sim" if linha["entrega"] else "não"

    ordem_ciclo = {"col": None, "desc": False}
    CHAVES_CICLO = {
        "pendencia": lambda l: (mod_ciclo.ORDEM_CICLO.get(l["pendencia"], 9), -l["idade"]),
        "pendente_em": lambda l: str(l["pendente_em"]).lower(),
        "tarefa": lambda l: int(l["tarefa"]) if str(l["tarefa"]).isdigit() else -1,
        "tipo": lambda l: str(l["tipo"]).lower(),
        "status": lambda l: str(l["status_wp"]).lower(),
        "entrega": lambda l: _entrega(l),
        "ramos": lambda l: ",".join(l["versoes"]),
        "principal": lambda l: _num_pr(l["pr_principal"]),
        "producao": lambda l: _num_pr(l["pr_producao"]),
        "homologacao": lambda l: _num_pr(l["pr_homologacao"]),
        "outros": lambda l: _num_pr(l["pr_outros"]),
        "build": lambda l: str(l["build"] or "").lower(),
        "dias": lambda l: l["idade"],
        "assunto": lambda l: str(l["assunto"]).lower(),
    }

    def linhas_filtradas():
        """Filtros de tela: autor, entrega ao cliente e versão pedida nos ramos.

        Todos sobre o resultado já carregado - trocar filtro não refaz consulta.
        """
        autor = autor_c_var.get()
        entrega = entrega_var.get()
        versao = versao_var.get()
        linhas = []
        for linha in ciclo_cache["linhas"]:
            if autor != TODOS and autor not in linha["autores"]:
                continue
            if entrega == "Sim" and not linha["entrega"]:
                continue
            if entrega == "Não" and linha["entrega"]:
                continue
            if versao != TODAS and versao not in linha["versoes"]:
                continue
            linhas.append(linha)
        return linhas

    def render_ciclo():
        linhas = linhas_filtradas()
        aplicar_ordem(linhas, ordem_ciclo, CHAVES_CICLO)
        ciclo_dados[:] = linhas
        grid_c.delete(*grid_c.get_children())
        for i, l in enumerate(linhas):
            grid_c.insert("", "end", iid=str(i), tags=(l["pendencia"],), values=(
                l["pendencia"], l["pendente_em"], l["tarefa"], l["tipo"][:14],
                l["status_wp"][:16], _entrega(l), ", ".join(l["versoes"]) or "-",
                l["pr_principal"], l["pr_producao"], l["pr_homologacao"],
                l["pr_outros"][:16], l["build"] or "-", l["idade"], l["assunto"],
            ))
        btn_excel.config(state="normal" if linhas else "disabled")
        contagem = {}
        for l in linhas:
            contagem[l["pendencia"]] = contagem.get(l["pendencia"], 0) + 1
        resumo = ("%d tarefa(s): %d pode(m) mergear, %d sem PR de produção, %d falta(m) em "
                  "outra versão, %d aguardando aprovação, %d sem build, %d parada(s)." % (
                      len(linhas), contagem.get(mod_ciclo.MERGEAR, 0),
                      contagem.get(mod_ciclo.SEM_PROD, 0), contagem.get(mod_ciclo.SEM_VERSAO, 0),
                      contagem.get(mod_ciclo.APROVAR, 0),
                      contagem.get(mod_ciclo.SEM_BUILD, 0), contagem.get(mod_ciclo.PARADO, 0)))
        status.config(text=resumo, fg=tema["texto"])
        return resumo

    entrega_combo.bind("<<ComboboxSelected>>", lambda _e: render_ciclo())
    versao_combo.bind("<<ComboboxSelected>>", lambda _e: render_ciclo())

    ligar_ordenacao(grid_c, colunas_c, titulos_c, ordem_ciclo, CHAVES_CICLO,
                    lambda: render_ciclo())

    def escolher_varios(titulo, opcoes, marcados):
        if not opcoes:
            messagebox.showinfo("Sem dados", "Clique em Carregar antes de escolher.", parent=root)
            return None
        janela = tk.Toplevel(root)
        janela.title(titulo)
        janela.transient(root)
        janela.grab_set()
        tk.Label(janela, text=titulo, padx=12, pady=8).pack(anchor="w")
        quadro = tk.Frame(janela, padx=16)
        quadro.pack(fill="both", expand=True)
        escolhas = {}
        # marcado sem diferenciar acento nem caixa: e assim que a regra compara,
        # e a caixa nao pode aparecer desmarcada com a regra valendo
        ja_marcados = {mod_ciclo.sem_acento(m) for m in marcados}
        for opcao in opcoes:
            var = tk.BooleanVar(value=mod_ciclo.sem_acento(opcao) in ja_marcados)
            escolhas[opcao] = var
            tk.Checkbutton(quadro, text=opcao, variable=var).pack(anchor="w")
        saida = {"ok": False}

        def confirmar():
            saida["ok"] = True
            janela.destroy()

        barra = tk.Frame(janela, pady=10)
        barra.pack()
        tk.Button(barra, text="OK", width=10, command=confirmar).pack(side="left", padx=4)
        tk.Button(barra, text="Cancelar", width=10, command=janela.destroy).pack(side="left")
        root.wait_window(janela)
        if not saida["ok"]:
            return None
        return [o for o, v in escolhas.items() if v.get()]

    def do_tipos():
        escolha = escolher_varios("Tipos de tarefa que exigem chegar na produção:",
                                  ciclo_cache["tipos"], state.get("tipos_exigem", []))
        if escolha is None:
            return
        state["tipos_exigem"] = escolha
        salvar()
        if ciclo_cache["linhas"]:
            do_carregar_ciclo()

    def do_status():
        # os status marcados como fechados na própria instância já contam por si;
        # aqui é só para incluir status intermediários (ex.: um "teste aprovado")
        ja = state.get("status_libera") or ciclo_cache.get("fechados", [])
        escolha = escolher_varios(
            "Status que indicam tarefa pronta para mergear -- os marcados como "
            "concluída no próprio OpenProject já contam automaticamente:",
            ciclo_cache["status"], ja)
        if escolha is None:
            return
        state["status_libera"] = escolha
        salvar()
        if ciclo_cache["linhas"]:
            do_carregar_ciclo()

    def concluir_ciclo():
        ciclo_cache["linhas"] = resultado_ciclo.get("linhas", [])
        ciclo_cache["tipos"] = resultado_ciclo.get("tipos_vistos", [])
        ciclo_cache["status"] = resultado_ciclo.get("status_vistos", [])
        ciclo_cache["fechados"] = resultado_ciclo.get("status_fechados", [])
        autores = resultado_ciclo.get("autores", [])
        autor_c_combo["values"] = [TODOS] + autores
        if autor_c_var.get() not in [TODOS] + autores:
            autor_c_var.set(resultado_ciclo.get("usuario") if
                            resultado_ciclo.get("usuario") in autores else TODOS)
        versoes = resultado_ciclo.get("versoes_vistas", [])
        versao_combo["values"] = [TODAS] + versoes
        if versao_var.get() not in [TODAS] + versoes:
            versao_var.set(TODAS)
        log("")
        log(render_ciclo())
        if not state.get("tipos_exigem"):
            log("Nenhum tipo marcado em 'Tipos que exigem produção...' - sem isso, nenhuma "
                "tarefa é cobrada por falta de PR de produção.")

    def do_carregar_ciclo():
        salvar()
        dias_p = dias_parado_var.get().strip() or "7"
        if not dias_p.isdigit():
            status.config(text="'Parado após' deve ser um número.", fg=tema["erro"])
            return
        dias_t = dias_tarefas_var.get().strip() or "180"
        if not dias_t.isdigit() or int(dias_t) < 1:
            status.config(text="'Tarefas dos últimos (dias)' deve ser um número.",
                          fg=tema["erro"])
            return

        def work():
            log("")
            log("--- repositórios ---")
            repos, locais = resolver_repos(repos_var.get(), repo_var.get().strip(), log)
            log("  %s" % ", ".join(repos))
            usuario, segredo = github_prs.credencial_escolhida(
                conta_var.get(), repo_var.get().strip() or None, repos[0])
            gh = github_prs.GitHub(segredo)
            log("")
            log("--- GitHub (conta: %s) ---" % (usuario or "?"))
            if usuario:
                root.after(0, lambda: rotulo_conta.config(text="conta em uso: %s" % usuario))
            prs = []
            for org_repo in repos:
                lote = gh.prs_abertos(org_repo)
                log("  %s: %d PR(s) aberto(s)" % (org_repo, len(lote)))
                prs.extend(lote)

            tarefas = {}
            url_op, projeto = separar_projeto(op_url_var.get())
            token = token_var.get().strip()
            if url_op and token:
                log("")
                log("--- OpenProject ---")
                cliente = OpenProject(url_op, token)
                log("  conectado como %s" % cliente.eu())
                try:
                    fechados = set(cliente.status_fechados())
                    log("  status de concluída (isClosed): %s" % (", ".join(sorted(fechados)) or "-"))
                except StepError as exc:
                    fechados = set()
                    log("  não consegui ler os status: %s" % exc)
                # os números que interessam são os das tarefas com PR aberto;
                # a query salva é só um complemento opcional
                numeros = sorted({mod_ciclo.tarefa_do_pr(p) for p in prs if mod_ciclo.tarefa_do_pr(p)})
                wps = cliente.work_packages_por_id(numeros)
                log("  %d tarefa(s) dos PRs abertos, %d encontrada(s)" % (len(numeros), len(wps)))
                if projeto:
                    # com projeto na URL a lista deixa de sair dos PRs abertos: o
                    # que importa e a tarefa, tenha PR aberto ou nao
                    ja = {str(w.get("id")) for w in wps}
                    do_projeto, truncou = cliente.work_packages_do_projeto(
                        projeto, dias=int(dias_t))
                    extras = [w for w in do_projeto if str(w.get("id")) not in ja]
                    log("  projeto '%s': %d tarefa(s) nos últimos %s dias, +%d fora dos PRs abertos"
                        % (projeto, len(do_projeto), dias_t, len(extras)))
                    if truncou:
                        log("  ATENÇÃO: bateu o teto de páginas - a lista do projeto está "
                            "incompleta. Reduza 'Tarefas dos últimos (dias)'.")
                    wps.extend(extras)
                query_id = query_var.get().strip()
                if query_id:
                    ja = {str(w.get("id")) for w in wps}
                    extras = [w for w in cliente.work_packages_da_query(query_id)
                              if str(w.get("id")) not in ja]
                    log("  query %s: +%d tarefa(s)" % (query_id, len(extras)))
                    wps.extend(extras)
                for wp in wps:
                    # o schema e por projeto+tipo: ler o de uma tarefa e aplicar
                    # em todas troca o nome dos campos das outras
                    campos = cliente.campos_customizados(wp, cliente.nomes_de_campos(wp))
                    build = next((v for k, v in campos.items()
                                  if str(k).strip().upper().startswith("X5")), "")
                    situacao = titulo_do_link(wp, "status")
                    tarefas[str(wp.get("id"))] = {
                        "tipo": titulo_do_link(wp, "type"),
                        "status": situacao,
                        "fechado": situacao in fechados,
                        "assunto": wp.get("subject", ""),
                        "build": build,
                        "entrega": campo_por_nome(campos, CAMPO_ENTREGA),
                        "ramos": campo_por_nome(campos, CAMPO_RAMOS, PREFIXO_RAMOS) or "",
                    }
                if tarefas:
                    exemplo = list(tarefas.values())[0]
                    log("  exemplo: tipo=%s status=%s" % (exemplo["tipo"], exemplo["status"]))
                pedem = [(n, d) for n, d in tarefas.items()
                         if mod_ciclo.verdadeiro(d.get("entrega"))]
                log("  %d tarefa(s) com 'Confirmar entrega ao cliente?' marcado" % len(pedem))
                for numero, dados in pedem[:8]:
                    log("    %s: ramos '%s' -> versões %s" % (
                        numero, dados.get("ramos") or "-",
                        ", ".join(mod_ciclo.versoes_do_texto(dados.get("ramos"))) or "nenhuma"))
                sem_campo = [n for n, d in tarefas.items() if d.get("entrega") is None]
                if len(sem_campo) == len(tarefas) and tarefas:
                    log("  ATENÇÃO: nenhuma tarefa trouxe o campo 'Confirmar entrega ao "
                        "cliente?'. Confira o nome do campo no OpenProject.")
            else:
                log("OpenProject não configurado: o tipo sai do título do PR.")

            log("")
            log("--- revisões dos PRs abertos ---")
            revisoes = gh.revisoes(prs, progresso=lambda i, n: log("  %d/%d" % (i, n)))
            aprovados = sum(1 for v in revisoes.values() if v == "aprovado")
            log("  %d aprovado(s), %d com pedido de ajuste" % (
                aprovados, sum(1 for v in revisoes.values() if v == "ajustes")))

            nome_prod = prod_c_var.get().strip() or prod_var.get().strip() or "producao"
            nome_main = main_var.get().strip() or "master"
            nome_homo = homo_c_var.get().strip()
            modelo = mod_ciclo.modelo_de_branch(nome_prod, nome_homo)

            # toda branch que alguma tarefa exige tem histórico lido: sem isso
            # não dá para dizer que o commit já está lá
            alvos = [n for n in (nome_main, nome_prod, nome_homo) if n]
            for dados in tarefas.values():
                if not mod_ciclo.verdadeiro(dados.get("entrega")):
                    continue
                for versao in mod_ciclo.versoes_do_texto(dados.get("ramos")):
                    nome = mod_ciclo.branch_da_versao(versao, modelo, (nome_prod, nome_homo))
                    if not any(mod_ciclo.mesma_branch(nome, ja) for ja in alvos):
                        alvos.append(nome)
            log("")
            log("--- branches analisadas: %s ---" % ", ".join(alvos))

            # histórico local: separa "mergeado" de "PR não aberto", por branch
            historico = {}
            if locais:
                log("")
                log("--- histórico local (para saber o que já foi mergeado) ---")
                for branch in alvos:
                    numeros = set()
                    achou = False
                    for nome_repo, caminho in locais.items():
                        ref = "origin/" + strip_origin(branch)
                        if not _ref_valida(caminho, ref):
                            log("  %s: %s não existe no clone" % (nome_repo, ref))
                            continue
                        _assuntos, ops, total = indice_producao(caminho, ref)
                        numeros |= set(ops)
                        achou = True
                        log("  %s %s: %d commits" % (nome_repo, ref, total))
                    historico[branch] = numeros if achou else None
            else:
                log("")
                log("Sem clone local informado: não dá para distinguir 'mergeado' de "
                    "'PR não aberto' (informe a pasta do clone no campo Repositórios).")
                if projeto:
                    log("ATENÇÃO: no modo projeto é o histórico do clone que responde "
                        "'o commit está na branch?'. Sem clone, toda branch obrigatória "
                        "aparece como pendente.")

            ignoradas = {}
            linhas = mod_ciclo.montar(
                prs, tarefas, nome_prod, nome_main,
                state.get("tipos_exigem", []), state.get("status_libera", []), int(dias_p),
                revisoes=revisoes, historico=historico,
                base_homologacao=nome_homo, modelo_branch=modelo,
                exigir_pr=not projeto, ignoradas=ignoradas,
            )
            log("")
            if projeto:
                log("--- modo projeto: %d tarefa(s) conferidas, com PR aberto ou sem ---"
                    % len(tarefas))
                if ignoradas.get("nao_entregues"):
                    log("  %d tarefa(s) faltam numa branch de versão mas também não estão "
                        "na principal: são trabalho não entregue, não backport atrasado."
                        % ignoradas["nao_entregues"])
            else:
                log("--- modo PR aberto: sem projeto na URL, o que foi mergeado e teve o PR "
                    "apagado não entra ---")
            resultado_ciclo["linhas"] = linhas
            resultado_ciclo["autores"] = sorted({p["autor"] for p in prs if p["autor"]})
            resultado_ciclo["usuario"] = usuario
            resultado_ciclo["tipos_vistos"] = mod_ciclo.tipos_vistos(linhas)
            resultado_ciclo["status_vistos"] = sorted(
                {l["status_wp"] for l in linhas if l["status_wp"] != "-"})
            resultado_ciclo["status_fechados"] = sorted(fechados) if url_op and token else []
            resultado_ciclo["versoes_vistas"] = sorted(
                {v for l in linhas for v in l["versoes"]})
            resultado_ciclo["branches"] = alvos

        in_thread(work, "Ciclo carregado.", depois=concluir_ciclo)

    def do_abrir_ciclo():
        for linha in selecionadas_c()[:4]:
            # /work_packages/<id> e da instancia, nao do projeto
            url_op, _projeto = separar_projeto(op_url_var.get())
            if linha["tarefa"] != "-" and url_op:
                webbrowser.open("%s/work_packages/%s" % (url_op, linha["tarefa"]), new=2)
            for pr in linha["prs"][:4]:
                if pr.get("url"):
                    webbrowser.open(pr["url"], new=2)

    def do_excel():
        """Exporta o que está na tela: uma aba igual à grade e uma por branch.

        A aba longa (uma linha por tarefa x branch) é a que responde 'o que falta
        na 2602?' com um filtro do Excel, sem ler texto de célula.
        """
        linhas = list(ciclo_dados)
        if not linhas:
            status.config(text="Nada carregado para exportar.", fg=tema["erro"])
            return
        caminho = filedialog.asksaveasfilename(
            parent=root, title="Exportar análise do ciclo",
            defaultextension=".xlsx", filetypes=[("Planilha do Excel", "*.xlsx")],
            initialfile="ciclo-backport.xlsx")
        if not caminho:
            return
        cabecalho = [titulos_c.get(c, c.upper()) for c in colunas_c]
        largas = {"pendente_em": 46, "assunto": 60, "principal": 30, "producao": 30,
                  "homologacao": 30, "outros": 22, "pendencia": 22, "status": 16,
                  "tipo": 14, "ramos": 12, "build": 18}
        larguras = [largas.get(c, 10) for c in colunas_c]
        tabela = [[
            l["pendencia"], l["pendente_em"], l["tarefa"], l["tipo"], l["status_wp"],
            _entrega(l), ", ".join(l["versoes"]) or "-", l["pr_principal"],
            l["pr_producao"], l["pr_homologacao"], l["pr_outros"], l["build"] or "-",
            l["idade"], l["assunto"],
        ] for l in linhas]
        try:
            excel.escrever(caminho, [
                ("Tarefas", cabecalho, tabela, larguras),
                ("Pendências por branch", list(mod_ciclo.COLUNAS_LONGO),
                 mod_ciclo.linhas_por_branch(linhas),
                 [10, 14, 16, 16, 12, 30, 10, 22, 12, 12, 60]),
            ])
        except Exception as exc:
            status.config(text="Não consegui gravar a planilha: %r" % (exc,), fg=tema["erro"])
            log("Falha ao exportar: %r" % (exc,))
            return
        status.config(text="Exportado: %s" % caminho, fg=tema["ok"])
        log("Planilha gravada em %s (%d tarefa(s), %d linha(s) por branch)." % (
            caminho, len(tabela), len(mod_ciclo.linhas_por_branch(linhas))))

    autor_c_combo.bind("<<ComboboxSelected>>", lambda _e: (salvar(), render_ciclo()))
    btn_carregar.config(command=do_carregar_ciclo)
    btn_tipos.config(command=do_tipos)
    btn_status.config(command=do_status)
    btn_excel.config(command=do_excel)
    btn_abrir_wp.config(command=do_abrir_ciclo)
    grid_c.bind("<Double-1>", lambda _e: do_abrir_ciclo())

    log("Aba 'Backport (git)': compara a principal com a produção e lista o que falta portar.")
    log("Aba 'Ciclo': cruza os PRs ABERTOS do GitHub com as tarefas do OpenProject.")
    log("Selecione uma linha para ver o detalhe da situação na barra de status.")
    def ao_fechar():
        # guarda o que está na tela (inclusive o token) mesmo sem clicar em nada
        try:
            salvar()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", ao_fechar)
    pump()
    root.mainloop()


if __name__ == "__main__":
    main()
