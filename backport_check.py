"""BackportCheck: o que esta na master e ainda nao chegou na branch de producao.

Compara `origin/<principal>` com `origin/<producao>` e lista o que falta portar,
classificando cada commit em quatro situacoes:

    PENDENTE  - nenhum sinal de que foi portado
    BRANCH    - ja existe branch de backport no origin, mas nada na producao
                (backport publicado e PR nao mergeado)
    PROVAVEL  - o numero da tarefa aparece em algum commit da producao, com outro
                assunto (tipico de PR de backport intitulado com o nome da branch)
    (portado) - patch-id equivalente ou assunto igual: nao entra na lista

O botao de backport reaproveita o fluxo do CherryPickPush: cria a branch a partir
de origin/<producao>, faz cherry-pick, push e abre a pagina do PR. Em conflito ele
para e nao empurra nada.
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

# Janela de historico lida na producao para decidir o que ja foi portado.
# Independe do filtro de dias da master: um backport pode ter sido feito bem depois.
JANELA_PRODUCAO_DIAS = 1095

PENDENTE = "PENDENTE"
BRANCH = "BRANCH CRIADA"
PROVAVEL = "PROVAVEL"

ORDEM = {PENDENTE: 0, BRANCH: 1, PROVAVEL: 2}

CORES = {PENDENTE: "#b00000", BRANCH: "#b06000", PROVAVEL: "#707070"}

LEGENDA = (
    (PENDENTE, "nenhum sinal de backport - e o que quebra cliente"),
    (BRANCH, "a branch de backport existe no origin, mas nada chegou na producao (PR nao mergeado)"),
    (PROVAVEL, "a OP ja aparece na producao com outro assunto - confira antes de descartar"),
)

RODAPE_LEGENDA = (
    "Ja portado (patch-id equivalente ou assunto igual) nao entra na lista.  |  "
    "Coluna CONFLITO: simulacao do cherry-pick sobre a producao, sem alterar nada no seu repo."
)

TODOS = "(todos)"

# coluna conflito
NAO_CHECADO = "..."
LIMPO = "limpo"
CONFLITO = "conflito"
SEM_INFO = "?"


# ---------------------------------------------------------------- normalizacao

def norm_assunto(texto):
    """Assunto comparavel: sem (#1234), sem acento, so alfanumerico."""
    texto = re.sub(r"\(#\d+\)", " ", texto)
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texto).split())


def ops_do_assunto(texto):
    """Numeros de OP do assunto. Tira antes o (#1234) do PR para nao confundir."""
    return set(re.findall(r"\b\d{5,7}\b", re.sub(r"\(#\d+\)", " ", texto)))


def pr_do_assunto(texto):
    achados = re.findall(r"\(#(\d+)\)", texto)
    return achados[-1] if achados else ""


def sufixo_producao(producao):
    """release-2601 -> 2601 ; v2.2701 -> 2701 ; fallback: nome sem pontuacao."""
    nome = strip_origin(producao)
    numeros = re.findall(r"\d+", nome)
    return numeros[-1] if numeros else re.sub(r"[^A-Za-z0-9]+", "", nome)


def nome_branch_padrao(op, sha, sufixo):
    return "fb_%s_%s" % (op or sha[:7], sufixo)


# ---------------------------------------------------------------- analise

def _ref_valida(repo, ref):
    return git_ok(repo, ["rev-parse", "-q", "--verify", ref + "^{commit}"])[0]


def analisar(repo, producao, principal, dias, log):
    """Devolve (linhas, portados_por_autor, autores).

    Nao filtra por autor: quem filtra e a tela, para trocar de autor sem
    reprocessar o git. Levanta StepError em erro de uso.
    """
    ensure_repo(repo, log)
    if not producao:
        raise StepError("Informe a branch de producao.")
    if not principal:
        raise StepError("Informe a branch principal.")

    log("")
    log("--- fetch ---")
    if run_git(repo, ["fetch", "origin", "--prune"], log)[0] != 0:
        raise StepError("git fetch falhou. Veja a saida acima.")

    ref_prod = "origin/" + strip_origin(producao)
    ref_main = "origin/" + strip_origin(principal)
    for ref in (ref_prod, ref_main):
        if not _ref_valida(repo, ref):
            raise StepError("Branch '%s' nao encontrada no origin depois do fetch." % ref)

    sufixo = sufixo_producao(producao)
    log("")
    log("--- lendo %s (ultimos %d dias) ---" % (ref_prod, JANELA_PRODUCAO_DIAS))
    assuntos_prod = set()
    ops_prod = {}
    ok, saida = git_ok(repo, [
        "log", "--format=%s", ref_prod, "--since=%d days ago" % JANELA_PRODUCAO_DIAS,
    ])
    if not ok:
        raise StepError("Nao foi possivel ler o historico de %s." % ref_prod)
    for linha in saida.splitlines():
        chave = norm_assunto(linha)
        if chave:
            assuntos_prod.add(chave)
        for op in ops_do_assunto(linha):
            ops_prod.setdefault(op, linha.strip())
    log("%d commits lidos na producao." % len(saida.splitlines()))

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
    log("--- comparando %s com %s (ultimos %s dias) ---" % (ref_main, ref_prod, dias))
    # --cherry-pick --right-only: so o que existe na master e nao tem equivalente
    # (mesmo patch-id) na producao. Pega o cherry-pick limpo; o que veio com
    # conflito resolvido diferente sobra e cai nas regras de assunto/OP abaixo.
    ok, saida = git_ok(repo, [
        "log", "--no-merges", "--cherry-pick", "--right-only",
        "--since=%s days ago" % dias, "--date=short",
        "--format=%H%x1f%an%x1f%ad%x1f%s",
        "%s...%s" % (ref_prod, ref_main),
    ])
    if not ok:
        raise StepError("Nao foi possivel comparar as branches. Confira os nomes.")

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
            detalhe = "OP %s ja aparece na producao: %s" % (comuns[0], ops_prod[comuns[0]])
        else:
            achadas = [
                b for b in branches_remotas
                if sufixo in b and any(op in b for op in ops)
            ]
            if achadas:
                status = BRANCH
                detalhe = "branch no origin sem merge na producao: %s" % ", ".join(achadas[:3])
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
    """Simula o cherry-pick de <sha> sobre a producao SEM tocar no working tree.

    `git merge-tree` faz o merge de 3 vias so em memoria: base = <sha>^,
    lados = ponta da producao e <sha>. Sai 0 limpo, 1 com conflito.
    Devolve (estado, arquivos_em_conflito).
    """
    if not git_ok(repo, ["rev-parse", "-q", "--verify", sha + "^{commit}"])[0]:
        return SEM_INFO, []
    if not git_ok(repo, ["rev-parse", "-q", "--verify", sha + "^^{commit}"])[0]:
        return SEM_INFO, []  # commit raiz: nao ha base para o merge de 3 vias
    ref = "origin/" + strip_origin(producao)
    codigo, saida = run_git(repo, [
        "merge-tree", "--write-tree", "--name-only", "--merge-base", sha + "^", ref, sha,
    ], lambda _m: None, quiet=True)
    if codigo == 0:
        return LIMPO, []
    if codigo != 1:
        return SEM_INFO, []
    # linha 1 = oid da arvore; depois, os arquivos ate a primeira linha em branco
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

    from cherrypick_tool import pr_compare_url, remote_web_url

    root = tk.Tk()
    root.title("BackportCheck - o que falta na producao")
    root.geometry("1180x790")
    root.minsize(940, 620)

    state = load_state()
    log_queue = queue.Queue()
    dados = []                                   # linhas visiveis, na ordem da grade
    cache = {"linhas": [], "portados": {}}       # resultado completo da ultima analise
    checagem = {"gen": 0}                        # geracao da checagem de conflito em curso

    def log(msg=""):
        log_queue.put(msg)

    topo = tk.Frame(root, padx=10, pady=8)
    topo.pack(fill="x")
    topo.columnconfigure(1, weight=1)

    tk.Label(topo, text="Repositorio").grid(row=0, column=0, sticky="w", pady=2)
    repo_var = tk.StringVar(value=state.get("repo", ""))
    tk.Entry(topo, textvariable=repo_var).grid(row=0, column=1, columnspan=3, sticky="ew", pady=2)

    def browse():
        caminho = filedialog.askdirectory(initialdir=repo_var.get() or os.path.expanduser("~"))
        if caminho:
            repo_var.set(caminho)

    tk.Button(topo, text="...", width=3, command=browse).grid(row=0, column=4, padx=(6, 0))

    tk.Label(topo, text="Branch de producao").grid(row=1, column=0, sticky="w", pady=2)
    prod_var = tk.StringVar(value=state.get("producao", ""))
    tk.Entry(topo, textvariable=prod_var, width=18).grid(row=1, column=1, sticky="w", pady=2)

    tk.Label(topo, text="Branch principal").grid(row=1, column=2, sticky="e", padx=(12, 6))
    main_var = tk.StringVar(value=state.get("principal", "master"))
    tk.Entry(topo, textvariable=main_var, width=18).grid(row=1, column=3, sticky="w")

    tk.Label(topo, text="Autor").grid(row=2, column=0, sticky="w", pady=2)
    autor_var = tk.StringVar(value=state.get("autor", "") or TODOS)
    autor_combo = ttk.Combobox(topo, textvariable=autor_var, width=26, state="readonly")
    autor_combo["values"] = [TODOS] + ([state["autor"]] if state.get("autor") else [])
    autor_combo.grid(row=2, column=1, sticky="w", pady=2)
    tk.Label(topo, text="(preenchido pela analise)", fg="#666").grid(
        row=2, column=2, sticky="e", padx=(12, 6)
    )

    tk.Label(topo, text="Dias").grid(row=2, column=3, sticky="w", padx=(120, 0))
    dias_var = tk.StringVar(value=str(state.get("dias", "180")))
    tk.Entry(topo, textvariable=dias_var, width=6).grid(row=2, column=3, sticky="e")

    btns = tk.Frame(root, padx=10)
    btns.pack(fill="x")
    btn_analisar = tk.Button(btns, text="Analisar", width=14)
    btn_analisar.pack(side="left")
    btn_backport = tk.Button(btns, text="Cherry-pick + push do selecionado", width=32, state="disabled")
    btn_backport.pack(side="left", padx=6)
    btn_pr = tk.Button(btns, text="Abrir PR de origem", width=18, state="disabled")
    btn_pr.pack(side="left")
    tk.Label(
        btns,
        text="(selecione varias linhas para levar tudo na mesma branch)",
        fg="#666",
    ).pack(side="left", padx=10)

    legenda = tk.Frame(root, padx=10, pady=6)
    legenda.pack(fill="x")
    tk.Label(legenda, text="Legenda:", fg="#333").grid(row=0, column=0, sticky="nw")
    for i, (situacao, texto) in enumerate(LEGENDA):
        tk.Label(
            legenda, text=situacao, fg=CORES[situacao], font=("TkDefaultFont", 9, "bold")
        ).grid(row=i, column=1, sticky="w", padx=(8, 6))
        tk.Label(legenda, text="= " + texto, fg="#444").grid(row=i, column=2, sticky="w")
    tk.Label(legenda, text=RODAPE_LEGENDA, fg="#777").grid(
        row=len(LEGENDA), column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(2, 0)
    )

    corpo = tk.Frame(root, padx=10, pady=8)
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

    out = scrolledtext.ScrolledText(root, height=9, wrap="none", state="disabled")
    out.pack(fill="both", expand=False, padx=10)

    status = tk.Label(root, text="Preencha e clique em Analisar.", anchor="w", fg="#333", padx=10)
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
        status.config(text="Executando...", fg="#333")

        def worker():
            try:
                fn()
                root.after(0, lambda: status.config(text=msg_ok, fg="#0a0"))
                if depois:
                    root.after(0, depois)
            except StepError as exc:
                log("")
                log("ERRO: %s" % exc)
                root.after(0, lambda: status.config(text=str(exc), fg="#c00"))
            except Exception as exc:
                log("")
                log("ERRO inesperado: %r" % (exc,))
                root.after(0, lambda: status.config(text="Erro inesperado: %r" % (exc,), fg="#c00"))
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
            status.config(text=linha["detalhe"], fg="#333")

    grid.bind("<<TreeviewSelect>>", on_select)

    def salvar():
        save_state({
            "repo": repo_var.get().strip(),
            "producao": prod_var.get().strip(),
            "principal": main_var.get().strip(),
            "autor": "" if autor_var.get() == TODOS else autor_var.get(),
            "dias": dias_var.get().strip() or "180",
        })

    def render():
        """Redesenha a grade a partir do cache, aplicando o filtro de autor."""
        autor = autor_var.get()
        if autor == TODOS:
            linhas = list(cache["linhas"])
            portados = sum(cache["portados"].values())
        else:
            linhas = [l for l in cache["linhas"] if l["autor"] == autor]
            portados = cache["portados"].get(autor, 0)
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
        resumo = "%d pendente(s), %d com branch criada, %d provavel(is); %d ja portado(s) fora da lista." % (
            contagem.get(PENDENTE, 0), contagem.get(BRANCH, 0),
            contagem.get(PROVAVEL, 0), portados,
        )
        status.config(text=resumo, fg="#333")
        checar_conflitos()
        return resumo

    def checar_conflitos():
        """Preenche a coluna conflito em segundo plano, sem travar os botoes."""
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
            root.after(0, lambda: log("Checagem de conflito concluida (%d commits)." % len(alvos)))

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
            status.config(text="Dias deve ser um numero.", fg="#c00")
            return
        salvar()

        def work():
            (resultado["linhas"], resultado["portados"],
             resultado["autores"]) = analisar(repo, producao, principal, dias, log)

        in_thread(work, "Analise concluida.", depois=concluir_analise)

    def do_backport():
        marcadas = selecionadas()
        if not marcadas:
            return
        repo = repo_var.get().strip()
        producao = prod_var.get().strip()
        # ordem cronologica de aplicacao
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
            "Atencao",
            "%s%sContinuar mesmo assim?" % (
                ("Sinal de que talvez ja tenham sido portados:\n%s\n\n" % "\n".join(avisos)) if avisos else "",
                ("O cherry-pick vai parar em conflito:\n%s\n\n" % "\n".join(conflitados)) if conflitados else "",
            ),
            parent=root,
        ):
            return

        log("")
        log("=== backport para %s ===" % final)
        in_thread(
            lambda: backportar(repo, producao, [m["sha"] for m in marcadas], final, log),
            "Backport publicado. Confira a pagina do PR no navegador.",
            depois=do_analisar,
        )

    def do_abrir_pr():
        marcadas = selecionadas()
        if not marcadas:
            return
        web = remote_web_url(repo_var.get().strip())
        if not web:
            status.config(text="Nao foi possivel identificar a url do origin.", fg="#c00")
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

    log("Compara a branch principal com a de producao e lista o que falta portar.")
    log("Selecione uma linha para ver o detalhe da situacao na barra de status.")
    pump()
    root.mainloop()


if __name__ == "__main__":
    main()
