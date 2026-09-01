"""Gera os executáveis e PROVA que eles carregam o código-fonte atual.

Timestamp novo não prova código novo: um build que reaproveita cache, ou o fonte
errado editado, produz um exe com data de hoje e comportamento velho. Aqui cada
módulo embutido no exe é comparado com o `.py` do disco pelo BYTECODE, não pelo
nome nem pela data. Mudança que não altera comportamento (comentário, formatação)
passa como "confere" de propósito: o que se verifica é o exe fazer o que o fonte
manda, não o arquivo ser byte a byte igual.

Rode com: python gerar_exes.py            (gera e verifica os dois)
          python gerar_exes.py --verificar (só verifica os exes que já existem)

Só stdlib + PyInstaller, como o resto do projeto.
"""

import io
import marshal
import os
import subprocess
import sys
import tempfile

RAIZ = os.path.dirname(os.path.abspath(__file__))

# (spec, exe, módulos próprios que TÊM de estar embutidos e conferir)
ALVOS = [
    ("BackportCheck.spec", os.path.join("dist", "BackportCheck.exe"),
     ("backport_check", "cherrypick_tool", "ciclo", "excel", "github_prs", "openproject")),
    ("CherryPickPush.spec", os.path.join("dist", "CherryPickPush.exe"),
     ("cherrypick_tool",)),
]


def digital(codigo):
    """Impressão digital do code object, ignorando o nome do arquivo.

    `co_filename` muda entre o fonte do disco e o que o PyInstaller embutiu, mas
    `co_code` e as constantes são idênticos quando o fonte é o mesmo - é o que
    transforma "o exe está novo" em fato verificável.
    """
    return (codigo.co_name, codigo.co_argcount, codigo.co_flags,
            codigo.co_names, codigo.co_varnames, codigo.co_code,
            tuple(digital(k) if hasattr(k, "co_code") else k for k in codigo.co_consts))


def codigo_do_fonte(modulo):
    caminho = os.path.join(RAIZ, modulo + ".py")
    if not os.path.exists(caminho):
        return None
    with io.open(caminho, encoding="utf-8") as fh:
        return compile(fh.read(), caminho, "exec", dont_inherit=True, optimize=0)


def codigos_do_exe(exe, modulos):
    """{módulo: code object} lido de dentro do exe.

    O script principal fica no CArchive; os módulos que ele importa, no PYZ - por
    isso os dois lugares são consultados.
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    car = CArchiveReader(exe)
    achados = {}
    for modulo in modulos:
        if modulo in car.toc:
            achados[modulo] = marshal.loads(car.extract(modulo))

    faltam = [m for m in modulos if m not in achados]
    if faltam:
        nomes_pyz = [n for n in car.toc if n.lower().endswith(".pyz")]
        if nomes_pyz:
            atalho = os.path.join(tempfile.gettempdir(), "gerar_exes.pyz")
            with open(atalho, "wb") as fh:
                fh.write(car.extract(nomes_pyz[0]))
            pyz = ZlibArchiveReader(atalho)
            for modulo in faltam:
                if modulo in pyz.toc:
                    bruto = pyz.extract(modulo)
                    achados[modulo] = (bruto if hasattr(bruto, "co_code")
                                       else marshal.loads(bruto))
    return achados


def verificar(exe, modulos):
    """Devolve (ok, linhas) comparando cada módulo do exe com o fonte do disco."""
    caminho = os.path.join(RAIZ, exe)
    if not os.path.exists(caminho):
        return False, ["  AUSENTE  o exe não existe: %s" % exe]

    embutidos = codigos_do_exe(caminho, modulos)
    ok, linhas = True, []
    for modulo in modulos:
        fonte = codigo_do_fonte(modulo)
        dentro = embutidos.get(modulo)
        if fonte is None:
            ok, situacao = False, "SEM FONTE"
        elif dentro is None:
            ok, situacao = False, "NAO EMBUTIDO"
        elif digital(dentro) != digital(fonte):
            ok, situacao = False, "DESATUALIZADO"
        else:
            situacao = "confere"
        linhas.append("  %-14s %s" % (situacao, modulo))
    return ok, linhas


def em_uso(exe):
    """True se o exe estiver aberto - o PyInstaller não consegue sobrescrever."""
    nome = os.path.basename(exe)
    try:
        saida = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + nome],
                               capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return False
    return nome.lower() in (saida or "").lower()


def gerar(spec):
    print(">> python -m PyInstaller --noconfirm %s" % spec)
    concluido = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", spec],
                               cwd=RAIZ, capture_output=True, text=True)
    if concluido.returncode != 0:
        cauda = (concluido.stderr or concluido.stdout or "").strip().splitlines()[-8:]
        print("   FALHOU:")
        for linha in cauda:
            print("     " + linha)
        return False
    return True


def dist_pendente():
    """Linhas do `git status` para dist/ - gerar não publica, dist/ é versionado."""
    try:
        saida = subprocess.run(["git", "status", "--porcelain", "dist"],
                               cwd=RAIZ, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return []
    return [l for l in (saida or "").splitlines() if l.strip()]


def main(argv):
    so_verificar = "--verificar" in argv
    problemas = []

    if not so_verificar:
        abertos = [exe for _spec, exe, _mods in ALVOS if em_uso(exe)]
        if abertos:
            print("Feche antes de gerar (o PyInstaller não sobrescreve exe aberto):")
            for exe in abertos:
                print("  " + os.path.basename(exe))
            return 1

    for spec, exe, modulos in ALVOS:
        print("=== %s" % os.path.basename(exe))
        if not so_verificar and not gerar(spec):
            problemas.append("%s: build falhou" % os.path.basename(exe))
            continue
        ok, linhas = verificar(exe, modulos)
        for linha in linhas:
            print(linha)
        if not ok:
            problemas.append("%s: o exe não corresponde ao fonte" % os.path.basename(exe))
        print()

    pendentes = dist_pendente()
    if pendentes:
        print("dist/ tem alteração NÃO commitada - os exes são versionados, gerar não publica:")
        for linha in pendentes:
            print("  " + linha)
        print()
        print("  git add dist && git commit -m \"Regenera os exes\" && git push")
    else:
        print("dist/ está igual ao commitado - nada para publicar.")

    if problemas:
        print()
        print("PROBLEMAS:")
        for p in problemas:
            print("  - " + p)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
