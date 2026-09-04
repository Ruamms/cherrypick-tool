"""Testes da regra do ciclo. Rode com: python -m unittest -v

Só stdlib, como o resto do projeto. Metade dos testes existe para travar o
comportamento ANTIGO (uma branch de produção, sem os campos novos do
OpenProject): a evolucao para múltiplas branches não pode mudar o que a
ferramenta já respondia.
"""

import datetime
import os
import unittest
import xml.etree.ElementTree as ET
import zipfile

import ciclo
import excel

HOJE = datetime.date(2026, 8, 31)
MAIN = "master"
PROD = "v2.2601"
HOMO = "v2.2602"


def pr(numero, base, tarefa="", atualizado="2026-08-29", autor="ruan",
       rascunho=False, titulo="", repo="org/repo"):
    return {
        "repo": repo, "numero": numero,
        "titulo": titulo or ("Ajuste - tarefa %s" % tarefa),
        "base": base, "head": ("fb_%s_%s" % (tarefa, base[-4:])) if tarefa else "algo",
        "autor": autor, "criado": "2026-08-01", "atualizado": atualizado,
        "rascunho": rascunho, "url": "https://github.com/org/repo/pull/%s" % numero,
    }


def tarefa(tipo, status="Desenvolvido", fechado=False, build="", entrega=None,
           ramos="", assunto="Assunto da tarefa", atribuido=""):
    return {"tipo": tipo, "status": status, "fechado": fechado, "assunto": assunto,
            "build": build, "entrega": entrega, "ramos": ramos,
            "atribuido": atribuido}


def uma(linhas, numero):
    return next(l for l in linhas if l["tarefa"] == numero)


def situacao(linha, branch):
    return next(l["situacao"] for l in linha["branches"] if l["nome"] == branch)


class TestVersoes(unittest.TestCase):
    def test_formatos_do_campo_ramos(self):
        casos = {
            "v2.2602": ["2602"],
            "2602": ["2602"],
            "v2.2602 - v2.2601": ["2602", "2601"],
            "2602 - 2601": ["2602", "2601"],
            "2602, 2601": ["2602", "2601"],
            "2602/2601": ["2602", "2601"],
            "  2602  2601 2602 ": ["2602", "2601"],
            "": [],
            "sem versão aqui": [],
        }
        for texto, esperado in casos.items():
            self.assertEqual(ciclo.versoes_do_texto(texto), esperado, texto)

    def test_nao_confunde_com_numero_de_tarefa(self):
        # 5 a 7 dígitos e tarefa, não versão: nada de recorte no meio do número
        self.assertEqual(ciclo.versoes_do_texto("tarefa 108692"), [])

    def test_modelo_sai_da_branch_configurada(self):
        self.assertEqual(ciclo.modelo_de_branch("v2.2601"), "v2.%s")
        self.assertEqual(ciclo.modelo_de_branch("release-2601"), "release-%s")
        self.assertEqual(ciclo.modelo_de_branch("", "v2.2602"), "v2.%s")
        self.assertEqual(ciclo.modelo_de_branch("master"), "%s")

    def test_branch_da_versao_prefere_o_nome_configurado(self):
        self.assertEqual(ciclo.branch_da_versao("2602", "v2.%s"), "v2.2602")
        self.assertEqual(
            ciclo.branch_da_versao("2601", "v2.%s", ("V2.2601", "v2.2602")), "V2.2601")

    def test_booleano_tolerante(self):
        for valor in (True, "sim", "Sim", "true", 1, "S"):
            self.assertTrue(ciclo.verdadeiro(valor), valor)
        for valor in (False, "nao", "Não", "false", 0, "", None):
            self.assertFalse(ciclo.verdadeiro(valor), valor)


class TestNumeroDaTarefa(unittest.TestCase):
    def test_le_o_numero_do_nome_do_branch(self):
        """`_` conta como caractere de palavra: sem trocar por espaço, o nome do
        branch nunca era lido e PR de título sem número ficava sem tarefa."""
        casos = {
            "fb_109866_2601": "109866",
            "fb_109866": "109866",
            "fb_108692_2502": "108692",
            "109866-ajuste": "109866",
            "release/fb_104328_2601": "104328",
        }
        for head, esperado in casos.items():
            achado = ciclo.tarefa_do_pr({"head": head, "titulo": "sem numero aqui"})
            self.assertEqual(achado, esperado, head)

    def test_titulo_serve_de_reserva(self):
        self.assertEqual(
            ciclo.tarefa_do_pr({"head": "ajuste-utf8", "titulo": "OP 110122 - encargos"}),
            "110122")

    def test_nao_confunde_o_numero_do_pr_do_squash(self):
        self.assertEqual(
            ciclo.tarefa_do_pr({"head": "ajuste", "titulo": "Corretiva (#108692)"}), "")

    def test_sufixo_de_versao_nao_e_tarefa(self):
        self.assertEqual(ciclo.tarefa_do_pr({"head": "fb_2601", "titulo": "x"}), "")

    def test_pr_sem_numero_nenhum(self):
        self.assertEqual(ciclo.tarefa_do_pr({"head": "utf8", "titulo": "Ajustes"}), "")


class TestBranchesObrigatorias(unittest.TestCase):
    def cfg(self, exigem=("Corretiva", "Divida Tecnica"), homo=HOMO):
        return {"principal": MAIN, "producao": PROD, "homologacao": homo,
                "tipos_exigem": list(exigem)}

    def test_regra_base_e_so_a_principal(self):
        self.assertEqual(
            ciclo.branches_obrigatorias("Evolutiva", tarefa("Evolutiva"), self.cfg()),
            [MAIN])

    def test_tipo_que_exige_producao_leva_as_duas_versoes(self):
        self.assertEqual(
            ciclo.branches_obrigatorias("Corretiva", tarefa("Corretiva"), self.cfg()),
            [MAIN, PROD, HOMO])

    def test_sem_homologacao_configurada_e_a_regra_antiga(self):
        self.assertEqual(
            ciclo.branches_obrigatorias("Corretiva", tarefa("Corretiva"),
                                        self.cfg(homo="")),
            [MAIN, PROD])

    def test_entrega_ao_cliente_adiciona_os_ramos(self):
        dados = tarefa("Adaptativa", entrega=True, ramos="2602 / 2601")
        self.assertEqual(
            ciclo.branches_obrigatorias("Adaptativa", dados, self.cfg()),
            [MAIN, HOMO, PROD])

    def test_entrega_ao_cliente_com_um_ramo_so(self):
        dados = tarefa("Adaptativa", entrega=True, ramos="2602")
        self.assertEqual(
            ciclo.branches_obrigatorias("Adaptativa", dados, self.cfg()), [MAIN, HOMO])

    def test_entrega_falsa_nao_adiciona_nada(self):
        dados = tarefa("Adaptativa", entrega=False, ramos="2602")
        self.assertEqual(
            ciclo.branches_obrigatorias("Adaptativa", dados, self.cfg()), [MAIN])

    def test_corretiva_com_ramos_nao_duplica_a_producao(self):
        dados = tarefa("Corretiva", entrega=True, ramos="2601")
        self.assertEqual(
            ciclo.branches_obrigatorias("Corretiva", dados, self.cfg()),
            [MAIN, PROD, HOMO])


class TestRegrasAntigas(unittest.TestCase):
    """Sem homologação e sem os campos novos, a saída tem de ser a de antes."""

    def montar(self, prs, tarefas, **kw):
        kw.setdefault("historico", {MAIN: set(), PROD: set()})
        return ciclo.montar(prs, tarefas, PROD, MAIN, ["Corretiva", "Divida Tecnica"],
                            [], dias_parado=7, hoje=HOJE, **kw)

    def test_corretiva_sem_pr_de_producao(self):
        linhas = self.montar([pr(8006, MAIN, "108692")],
                             {"108692": tarefa("Corretiva")})
        linha = uma(linhas, "108692")
        self.assertEqual(linha["pendencia"], ciclo.SEM_PROD)
        self.assertEqual(linha["pr_producao"], ciclo.SEM_PR)

    def test_evolutiva_nao_e_cobrada_por_producao(self):
        linhas = self.montar([pr(8085, MAIN, "109757", atualizado="2026-08-30")],
                             {"109757": tarefa("Evolutiva")})
        self.assertEqual(uma(linhas, "109757")["pendencia"], ciclo.OK)

    def test_pr_de_producao_aberto_e_aguarda_aprovacao(self):
        linhas = self.montar([pr(7973, PROD, "109453", atualizado="2026-07-24")],
                             {"109453": tarefa("Divida Tecnica")})
        linha = uma(linhas, "109453")
        self.assertEqual(linha["pendencia"], ciclo.APROVAR)
        self.assertIn("aguardando aprovação", linha["pr_producao"])

    def test_concluida_com_pr_aberto_pode_mergear(self):
        linhas = self.montar([pr(8030, MAIN, "109866")],
                             {"109866": tarefa("Adaptativa", status="Testado",
                                               fechado=True)})
        self.assertEqual(uma(linhas, "109866")["pendencia"], ciclo.MERGEAR)

    def test_parado_por_idade(self):
        linhas = self.montar([pr(8010, MAIN, atualizado="2026-08-04")], {})
        self.assertEqual(linhas[0]["pendencia"], ciclo.PARADO)
        self.assertEqual(linhas[0]["idade"], 27)

    def test_falta_build_sem_pr_aberto(self):
        linhas = self.montar([], {"104328": tarefa("Adaptativa", fechado=True)})
        linha = uma(linhas, "104328")
        self.assertEqual(linha["pendencia"], ciclo.SEM_BUILD)
        self.assertEqual(linha["prs"], [])

    def test_mergeado_vem_do_historico_local(self):
        linhas = self.montar([pr(7857, MAIN, "108241")],
                             {"108241": tarefa("Corretiva")},
                             historico={MAIN: {"108241"}, PROD: {"108241"}})
        self.assertEqual(uma(linhas, "108241")["pr_producao"], ciclo.MERGEADO)

    def test_sem_clone_local_nao_afirma_nada(self):
        linhas = self.montar([pr(7857, MAIN, "108241")],
                             {"108241": tarefa("Corretiva")},
                             historico={MAIN: None, PROD: None})
        self.assertEqual(uma(linhas, "108241")["pr_producao"], ciclo.NAO_CONFERIDO)

    def test_rascunho_e_revisao(self):
        prs = [pr(1, PROD, "100001"), pr(2, PROD, "100002", rascunho=True)]
        linhas = ciclo.montar(prs, {}, PROD, MAIN, [], [], hoje=HOJE,
                              revisoes={("org/repo", 1): "aprovado"},
                              historico={MAIN: set(), PROD: set()})
        self.assertIn(ciclo.APROVADO, uma(linhas, "100001")["pr_producao"])
        self.assertIn(ciclo.RASCUNHO, uma(linhas, "100002")["pr_producao"])

    def test_agrupa_as_duas_pontas_da_mesma_tarefa(self):
        linhas = self.montar([pr(7857, MAIN, "108241"), pr(7860, PROD, "108241")],
                             {"108241": tarefa("Corretiva")})
        self.assertEqual(len(linhas), 1)
        self.assertEqual(len(linhas[0]["prs"]), 2)

    def test_pr_em_branch_estranha_cai_em_outros(self):
        linhas = self.montar([pr(7860, "v2.2502", "108241")],
                             {"108241": tarefa("Corretiva")})
        self.assertIn("#7860(v2.2502)", uma(linhas, "108241")["pr_outros"])


class TestMultiplasBranches(unittest.TestCase):
    def montar(self, prs, tarefas, historico=None, exigem=("Corretiva", "Divida Tecnica")):
        historico = historico or {MAIN: set(), PROD: set(), HOMO: set()}
        return ciclo.montar(prs, tarefas, PROD, MAIN, list(exigem), [],
                            dias_parado=7, hoje=HOJE, historico=historico,
                            base_homologacao=HOMO)

    def test_corretiva_e_cobrada_nas_duas_versoes(self):
        """Item 6: 108692 corretiva, PR na 2601 aguardando, nada na 2602."""
        linhas = self.montar([pr(8006, PROD, "108692", atualizado="2026-07-30")],
                             {"108692": tarefa("Corretiva")})
        linha = uma(linhas, "108692")
        self.assertEqual(linha["obrigatorias"], [MAIN, PROD, HOMO])
        self.assertIn("aguardando aprovação", situacao(linha, PROD))
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertIn("v2.2602: PR não aberto", linha["pendente_em"])

    def test_adaptativa_com_entrega_e_dois_ramos(self):
        """Item 13: 109866 adaptativa, entrega=True, ramos 2602/2601."""
        linhas = self.montar(
            [pr(8030, PROD, "109866", atualizado="2026-08-12")],
            {"109866": tarefa("Adaptativa", entrega=True, ramos="2602 / 2601")},
            historico={MAIN: {"109866"}, PROD: set(), HOMO: set()})
        linha = uma(linhas, "109866")
        self.assertEqual(sorted(linha["obrigatorias"]), [MAIN, PROD, HOMO])
        self.assertIn("aguardando aprovação", situacao(linha, PROD))
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertNotIn(MAIN, linha["pendente_em"])
        self.assertIn("v2.2601: aguardando", linha["pendente_em"])
        self.assertIn("v2.2602: PR não aberto", linha["pendente_em"])

    def test_evolutiva_sem_entrega_nao_cria_pendencia_de_versao(self):
        """Item 14: 109757 evolutiva, entrega=False -> só a principal."""
        linhas = self.montar(
            [pr(8085, MAIN, "109757", atualizado="2026-08-26")],
            {"109757": tarefa("Evolutiva", entrega=False)})
        linha = uma(linhas, "109757")
        self.assertEqual(linha["obrigatorias"], [MAIN])
        self.assertEqual(situacao(linha, PROD), ciclo.NAO_SOLICITADO)
        self.assertEqual(situacao(linha, HOMO), ciclo.NAO_SOLICITADO)
        self.assertEqual(linha["pendencia"], ciclo.OK)

    def test_entrega_so_na_homologacao_marca_producao_como_nao_solicitada(self):
        """Item 15: ramos 2602 -> a 2601 não e cobrada."""
        linhas = self.montar(
            [pr(8100, MAIN, "108777")],
            {"108777": tarefa("Adaptativa", entrega=True, ramos="2602")},
            historico={MAIN: {"108777"}, PROD: set(), HOMO: set()})
        linha = uma(linhas, "108777")
        self.assertEqual(linha["obrigatorias"], [MAIN, HOMO])
        self.assertEqual(situacao(linha, PROD), ciclo.NAO_SOLICITADO)
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        # o PR aberto na principal também e pendência: ainda falta mergear
        self.assertIn("v2.2602: PR não aberto", linha["pendente_em"])
        self.assertNotIn(PROD, linha["pendente_em"])

    def test_mergeado_em_uma_e_faltando_na_outra(self):
        """Item 7: 2601 mergeado, 2602 sem PR -> pendência só na 2602.

        Sem nenhum PR aberto a tarefa só entra pela regra do build vazio - e
        mesmo por esse caminho ela tem de trazer a situação de cada branch.
        """
        linhas = self.montar(
            [], {"108692": tarefa("Corretiva", fechado=True, build="")},
            historico={MAIN: {"108692"}, PROD: {"108692"}, HOMO: set()})
        linha = uma(linhas, "108692")
        self.assertEqual(situacao(linha, MAIN), ciclo.MERGEADO)
        self.assertEqual(situacao(linha, PROD), ciclo.MERGEADO)
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        self.assertEqual(linha["pendente_em"], "v2.2602: PR não aberto")

    def test_ramo_fora_das_branches_nomeadas(self):
        dados = tarefa("Adaptativa", entrega=True, ramos="2502")
        linhas = self.montar([pr(8200, MAIN, "108888")], {"108888": dados},
                             historico={MAIN: {"108888"}, PROD: set(), HOMO: set(),
                                        "v2.2502": set()})
        linha = uma(linhas, "108888")
        self.assertIn("v2.2502", linha["obrigatorias"])
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertIn("v2.2502: PR não aberto", linha["pendente_em"])

    def test_pr_de_homologacao_aberto_aguarda_aprovacao(self):
        linhas = self.montar(
            [pr(8300, HOMO, "109900", atualizado="2026-08-01")],
            {"109900": tarefa("Adaptativa", entrega=True, ramos="2602")},
            historico={MAIN: {"109900"}, PROD: set(), HOMO: set()})
        linha = uma(linhas, "109900")
        self.assertEqual(linha["pendencia"], ciclo.APROVAR)
        self.assertIn("aguardando aprovação", linha["pr_homologacao"])

    def test_ramos_com_versao_ilegivel_nao_inventa_branch(self):
        linhas = self.montar(
            [pr(8400, MAIN, "109901")],
            {"109901": tarefa("Adaptativa", entrega=True, ramos="próxima release")},
            historico={MAIN: {"109901"}, PROD: set(), HOMO: set()})
        self.assertEqual(uma(linhas, "109901")["obrigatorias"], [MAIN])

    def test_campo_ausente_no_openproject_nao_quebra(self):
        linhas = self.montar([pr(8500, MAIN, "109902")],
                             {"109902": {"tipo": "Corretiva", "status": "Desenvolvido"}})
        linha = uma(linhas, "109902")
        self.assertFalse(linha["entrega"])
        self.assertEqual(linha["versoes"], [])
        self.assertEqual(linha["obrigatorias"], [MAIN, PROD, HOMO])


class TestPadraoDosTipos(unittest.TestCase):
    """Sem escolha salva, os tipos de manutenção já vêm marcados - a ferramenta
    tem de servir para algo na primeira abertura."""

    def cfg(self):
        return {"principal": MAIN, "producao": PROD, "homologacao": HOMO,
                "tipos_exigem": list(ciclo.TIPOS_PADRAO)}

    def test_o_padrao_casa_com_a_grafia_do_gerenciador(self):
        for tipo in ("Corretiva", "corretiva", "Dívida Técnica", "divida tecnica"):
            self.assertEqual(
                ciclo.branches_obrigatorias(tipo, tarefa(tipo), self.cfg()),
                [MAIN, PROD, HOMO], tipo)

    def test_o_padrao_nao_cobra_funcionalidade_nova(self):
        for tipo in ("Evolutiva", "Adaptativa", "Análise", "Sugestão"):
            self.assertEqual(
                ciclo.branches_obrigatorias(tipo, tarefa(tipo), self.cfg()), [MAIN], tipo)

    def test_padrao_sem_acento_de_proposito(self):
        # a comparação é normalizada; acentuar aqui faria o casamento parar
        for tipo in ciclo.TIPOS_PADRAO:
            self.assertEqual(tipo, ciclo.sem_acento(tipo), tipo)


class TestFiltroEntrega(unittest.TestCase):
    def test_rotulo_de_cada_caso(self):
        self.assertEqual(ciclo.rotulo_entrega({"tem_entrega": True, "entrega": True}),
                         ciclo.ENTREGA_SIM)
        self.assertEqual(ciclo.rotulo_entrega({"tem_entrega": True, "entrega": False}),
                         ciclo.ENTREGA_NAO)
        self.assertEqual(ciclo.rotulo_entrega({"tem_entrega": False, "entrega": False}),
                         ciclo.ENTREGA_VAZIO)
        self.assertEqual(ciclo.rotulo_entrega({}), ciclo.ENTREGA_VAZIO)

    def test_rotulo_sai_da_linha_montada(self):
        linhas = ciclo.montar(
            [pr(8030, MAIN, "109866")],
            {"109866": tarefa("Adaptativa", entrega=True, ramos="2602")},
            PROD, MAIN, [], [], hoje=HOJE, base_homologacao=HOMO,
            historico={MAIN: set(), PROD: set(), HOMO: set()})
        self.assertEqual(ciclo.rotulo_entrega(uma(linhas, "109866")), ciclo.ENTREGA_SIM)


class TestUrlDoProjeto(unittest.TestCase):
    def test_separa_instancia_e_projeto(self):
        import openproject
        casos = {
            "https://op.empresa.com.br/": ("https://op.empresa.com.br", ""),
            "https://op.empresa.com.br": ("https://op.empresa.com.br", ""),
            "https://op.empresa.com.br/projects/meu-time":
                ("https://op.empresa.com.br", "meu-time"),
            "https://op.empresa.com.br/projects/meu-time/work_packages?query_id=42":
                ("https://op.empresa.com.br", "meu-time"),
            "https://op.empresa.com.br/projects/123": ("https://op.empresa.com.br", "123"),
            "": ("", ""),
        }
        for url, esperado in casos.items():
            self.assertEqual(openproject.separar_projeto(url), esperado, url)


class TestSemPrAberto(unittest.TestCase):
    """O caso que motivou tudo: PR para a master mergeado e apagado. Sem PR
    aberto, o radar de PR não vê nada - quem responde é o histórico do git."""

    def montar(self, tarefas, historico, exigir_pr, ignoradas=None):
        return ciclo.montar(
            [], tarefas, PROD, MAIN, ["Corretiva", "Divida Tecnica"], [],
            dias_parado=7, hoje=HOJE, historico=historico, base_homologacao=HOMO,
            exigir_pr=exigir_pr, ignoradas=ignoradas)

    def test_corretiva_na_master_e_faltando_nas_duas_versoes(self):
        linhas = self.montar(
            {"108692": tarefa("Corretiva", build="Banco: 2.2602.0.67")},
            {MAIN: {"108692"}, PROD: set(), HOMO: set()}, exigir_pr=False)
        linha = uma(linhas, "108692")
        self.assertEqual(linha["pendencia"], ciclo.SEM_PROD)
        self.assertEqual(situacao(linha, MAIN), ciclo.MERGEADO)
        self.assertEqual(situacao(linha, PROD), ciclo.SEM_PR)
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        self.assertIn("v2.2601: PR não aberto", linha["pendente_em"])
        self.assertIn("v2.2602: PR não aberto", linha["pendente_em"])

    def test_o_modo_antigo_nao_veria_essa_tarefa(self):
        """Mesmos dados, exigir_pr=True: é o comportamento de antes."""
        linhas = self.montar(
            {"108692": tarefa("Corretiva", build="Banco: 2.2602.0.67")},
            {MAIN: {"108692"}, PROD: set(), HOMO: set()}, exigir_pr=True)
        self.assertEqual(linhas, [])

    def test_entrega_ao_cliente_sem_pr_aberto(self):
        linhas = self.montar(
            {"109866": tarefa("Adaptativa", entrega=True, ramos="2602",
                              build="Finanças: 2.2601.9")},
            {MAIN: {"109866"}, PROD: set(), HOMO: set()}, exigir_pr=False)
        linha = uma(linhas, "109866")
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertEqual(linha["pendente_em"], "v2.2602: PR não aberto")
        self.assertEqual(situacao(linha, PROD), ciclo.NAO_SOLICITADO)

    def test_ja_esta_em_todas_as_obrigatorias_nao_aparece(self):
        linhas = self.montar(
            {"108692": tarefa("Corretiva", build="Banco: 2.2602.0.67")},
            {MAIN: {"108692"}, PROD: {"108692"}, HOMO: {"108692"}}, exigir_pr=False)
        self.assertEqual(linhas, [])

    def test_nem_na_master_esta_e_trabalho_nao_entregue(self):
        """Falta em tudo: não é backport atrasado, é tarefa que não terminou.
        Fica fora da lista, mas contada - sem corte silencioso."""
        ignoradas = {}
        linhas = self.montar(
            {"108692": tarefa("Corretiva", build="Banco: 2.2602.0.67")},
            {MAIN: set(), PROD: set(), HOMO: set()}, exigir_pr=False,
            ignoradas=ignoradas)
        self.assertEqual(linhas, [])
        self.assertEqual(ignoradas["nao_entregues"], 1)

    def test_build_vazio_continua_valendo_no_modo_projeto(self):
        linhas = self.montar(
            {"104328": tarefa("Adaptativa", fechado=True)},
            {MAIN: {"104328"}, PROD: set(), HOMO: set()}, exigir_pr=False)
        self.assertEqual(uma(linhas, "104328")["pendencia"], ciclo.SEM_BUILD)

    def test_pr_aberto_de_outra_tarefa_nao_interfere(self):
        linhas = ciclo.montar(
            [pr(8006, MAIN, "108000")],
            {"108000": tarefa("Corretiva"), "108692": tarefa("Corretiva")},
            PROD, MAIN, ["Corretiva"], [], hoje=HOJE, base_homologacao=HOMO,
            historico={MAIN: {"108692"}, PROD: set(), HOMO: set()},
            exigir_pr=False)
        self.assertEqual(uma(linhas, "108692")["pendencia"], ciclo.SEM_PROD)
        self.assertEqual(uma(linhas, "108000")["pendencia"], ciclo.SEM_PROD)


class TestExplicacoes(unittest.TestCase):
    """O balão da grade lê estas funções: a legenda saiu da tela e virou hover."""

    def test_explica_a_frase_mesmo_com_pr_e_dias_colados(self):
        self.assertIn("ninguém encostou",
                      ciclo.explicar_situacao("aguardando aprovação (19d) #8030"))
        self.assertIn("histórico", ciclo.explicar_situacao(ciclo.MERGEADO))
        self.assertIn("não é obrigatória", ciclo.explicar_situacao(ciclo.NAO_SOLICITADO))
        self.assertEqual(ciclo.explicar_situacao(""), "")

    def test_toda_situacao_tem_explicacao(self):
        frases = (ciclo.MERGEADO, ciclo.APROVADO, ciclo.AJUSTES, ciclo.AGUARDANDO,
                  ciclo.COMENTADO, ciclo.RASCUNHO, ciclo.SEM_PR, ciclo.NAO_CONFERIDO,
                  ciclo.NAO_SOLICITADO)
        for frase in frases:
            self.assertTrue(ciclo.explicar_situacao(frase), frase)

    def test_toda_pendencia_tem_explicacao(self):
        for pendencia in ciclo.ORDEM_CICLO:
            self.assertTrue(ciclo.explicar_pendencia(pendencia), pendencia)


class TestFormatoLongo(unittest.TestCase):
    def test_uma_linha_por_tarefa_e_branch(self):
        linhas = ciclo.montar(
            [pr(8030, PROD, "109866")],
            {"109866": tarefa("Adaptativa", entrega=True, ramos="2602 - 2601")},
            PROD, MAIN, [], [], hoje=HOJE, base_homologacao=HOMO,
            historico={MAIN: set(), PROD: set(), HOMO: set()})
        longo = ciclo.linhas_por_branch(linhas)
        self.assertEqual(len(longo), 3)
        self.assertEqual([l[3] for l in longo], [MAIN, PROD, HOMO])
        self.assertEqual(len(longo[0]), len(ciclo.COLUNAS_LONGO))
        por_branch = {l[3]: l for l in longo}
        self.assertEqual(por_branch[HOMO][4], "sim")     # obrigatória
        self.assertEqual(por_branch[HOMO][6], "sim")     # pendente
        self.assertEqual(por_branch[HOMO][8], "sim")     # entrega ao cliente

    def test_valor_negativo_sai_acentuado(self):
        """O "não" da planilha e da grade e texto visível: tem de vir acentuado."""
        linhas = ciclo.montar(
            [pr(8085, MAIN, "109757")],
            {"109757": tarefa("Evolutiva", entrega=False)},
            PROD, MAIN, [], [], hoje=HOJE, base_homologacao=HOMO,
            historico={MAIN: set(), PROD: set(), HOMO: set()})
        por_branch = {l[3]: l for l in ciclo.linhas_por_branch(linhas)}
        self.assertEqual(por_branch[HOMO][4], "não")     # obrigatória
        self.assertEqual(por_branch[HOMO][6], "não")     # pendente
        self.assertEqual(por_branch[HOMO][8], "não")     # entrega ao cliente


class TestTiposVistos(unittest.TestCase):
    """A lista de 'Tipos que exigem produção' mostrava 'Corretiva' e 'corretiva'
    como duas opções: a primeira vem do gerenciador, a segunda é deduzida do
    título do PR. Para a regra sempre foram o mesmo tipo."""

    def test_mesma_grafia_do_gerenciador_ganha(self):
        linhas = [{"tipo": "corretiva", "origem_tipo": "título do PR"},
                  {"tipo": "Corretiva", "origem_tipo": "gerenciador"}]
        self.assertEqual(ciclo.tipos_vistos(linhas), ["Corretiva"])

    def test_ordem_do_gerenciador_nao_importa(self):
        linhas = [{"tipo": "Corretiva", "origem_tipo": "gerenciador"},
                  {"tipo": "corretiva", "origem_tipo": "título do PR"}]
        self.assertEqual(ciclo.tipos_vistos(linhas), ["Corretiva"])

    def test_acento_tambem_e_o_mesmo_tipo(self):
        linhas = [{"tipo": "divida tecnica", "origem_tipo": "título do PR"},
                  {"tipo": "Dívida Técnica", "origem_tipo": "gerenciador"}]
        self.assertEqual(ciclo.tipos_vistos(linhas), ["Dívida Técnica"])

    def test_sem_gerenciador_fica_o_deduzido(self):
        linhas = [{"tipo": "corretiva", "origem_tipo": "título do PR"}]
        self.assertEqual(ciclo.tipos_vistos(linhas), ["corretiva"])

    def test_ignora_o_tipo_nao_identificado(self):
        linhas = [{"tipo": "-", "origem_tipo": "não identificado"},
                  {"tipo": "", "origem_tipo": "não identificado"},
                  {"tipo": "Análise", "origem_tipo": "gerenciador"}]
        self.assertEqual(ciclo.tipos_vistos(linhas), ["Análise"])

    def test_da_carga_real_sai_uma_opcao_por_tipo(self):
        prs = [pr(8006, MAIN, "108692", titulo="Corretiva - OP 108692 - copiar perfil"),
               pr(8010, MAIN, "108693", titulo="Corretiva - OP 108693 - outro erro")]
        linhas = ciclo.montar(prs, {"108692": tarefa("Corretiva")}, PROD, MAIN,
                              ["Corretiva"], [], hoje=HOJE,
                              historico={MAIN: set(), PROD: set()})
        # 108693 não está no gerenciador: o tipo dela sai do título, minúsculo
        self.assertEqual(sorted(l["tipo"] for l in linhas), ["Corretiva", "corretiva"])
        self.assertEqual(ciclo.tipos_vistos(linhas), ["Corretiva"])

    def test_as_duas_grafias_continuam_sendo_cobradas(self):
        """Marcar 'Corretiva' tem de cobrar produção nas duas grafias."""
        cfg = {"principal": MAIN, "producao": PROD, "homologacao": "",
               "tipos_exigem": ["Corretiva"]}
        for grafia in ("Corretiva", "corretiva", "CORRETIVA"):
            self.assertEqual(
                ciclo.branches_obrigatorias(grafia, tarefa(grafia), cfg),
                [MAIN, PROD], grafia)


class TestChavesSemAcento(unittest.TestCase):
    """As chaves comparadas DEPOIS de remover acento tem de ficar sem acento.

    Acentuar uma delas nao quebra nada visível - so faz o casamento parar de
    achar o tipo/campo, em silêncio. Daí o teste.
    """

    def test_tipos_conhecidos_sem_acento(self):
        for tipo in ciclo.TIPOS_CONHECIDOS:
            self.assertEqual(tipo, ciclo.sem_acento(tipo), tipo)

    def test_nomes_de_campo_sem_acento(self):
        import openproject
        for nome in openproject.CAMPO_ENTREGA + openproject.CAMPO_RAMOS:
            self.assertEqual(nome, ciclo.sem_acento(nome), nome)
        self.assertEqual(openproject.PREFIXO_RAMOS,
                         ciclo.sem_acento(openproject.PREFIXO_RAMOS))

    def test_casa_com_o_nome_acentuado_do_gerenciador(self):
        import openproject
        self.assertEqual(ciclo.tipo_do_titulo("Dívida Técnica"), "divida tecnica")
        self.assertEqual(ciclo.tipo_do_titulo("Divida Tecnica"), "divida tecnica")
        campos = {"Confirmar entrega ao cliente?": True,
                  "Ramos para disponibilização": "2602 - 2601"}
        self.assertIs(openproject.campo_por_nome(campos, openproject.CAMPO_ENTREGA), True)
        self.assertEqual(
            openproject.campo_por_nome(campos, openproject.CAMPO_RAMOS,
                                       openproject.PREFIXO_RAMOS),
            "2602 - 2601")

    def test_valores_booleanos_aceitos_sem_acento(self):
        for valor in ("sim", "Sim", "SIM"):
            self.assertTrue(ciclo.verdadeiro(valor), valor)
        for valor in ("não", "Não", "nao", "NAO"):
            self.assertFalse(ciclo.verdadeiro(valor), valor)


class TestProducaoExigeHomologacao(unittest.TestCase):
    """Tudo que está na produção tem de estar na homologação, QUALQUER tipo: a
    versão seguinte não pode sair sem o que o cliente já recebeu. Antes só o
    tipo mandava, e Adaptativa na 2601 sem 2602 não era cobrada de ninguém."""

    def cfg(self, exigem=("Corretiva",)):
        return {"principal": MAIN, "producao": PROD, "homologacao": HOMO,
                "tipos_exigem": list(exigem)}

    def montar(self, tarefas, historico, prs=(), **kw):
        kw.setdefault("exigir_pr", False)
        return ciclo.montar(list(prs), tarefas, PROD, MAIN, ["Corretiva"], [],
                            dias_parado=7, hoje=HOJE, historico=historico,
                            base_homologacao=HOMO, **kw)

    def test_adaptativa_na_producao_passa_a_ser_cobrada(self):
        linhas = self.montar({"106185": tarefa("Adaptativa")},
                             {MAIN: {"106185"}, PROD: {"106185"}, HOMO: set()})
        linha = uma(linhas, "106185")
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertIn(HOMO, linha["obrigatorias"])

    def test_fora_da_producao_a_regra_do_tipo_continua_valendo(self):
        linhas = self.montar({"106185": tarefa("Adaptativa")},
                             {MAIN: {"106185"}, PROD: set(), HOMO: set()})
        self.assertEqual([l["tarefa"] for l in linhas], [])

    def test_na_producao_sem_a_principal_nao_e_descartada(self):
        # o caso 110094: entrou por backport e nunca passou na principal
        ignoradas = {}
        linhas = self.montar({"110094": tarefa("Corretiva")},
                             {MAIN: set(), PROD: {"110094"}, HOMO: set()},
                             ignoradas=ignoradas)
        linha = uma(linhas, "110094")
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertIn("está na produção", linha["detalhe"])
        self.assertFalse(ignoradas.get("nao_entregues"))

    def test_vale_tambem_com_pr_aberto(self):
        linhas = ciclo.montar(
            [pr(8100, MAIN, "109618")], {"109618": tarefa("Adaptativa")},
            PROD, MAIN, ["Corretiva"], [], dias_parado=7, hoje=HOJE,
            historico={MAIN: set(), PROD: {"109618"}, HOMO: set()},
            base_homologacao=HOMO)
        linha = uma(linhas, "109618")
        self.assertIn(HOMO, linha["obrigatorias"])
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)

    def test_sem_clone_local_nao_inventa_obrigacao(self):
        # histórico None é "não conferido"; falta de informação não vira regra
        self.assertEqual(
            ciclo.branches_obrigatorias(
                "Adaptativa", tarefa("Adaptativa"), self.cfg(),
                ciclo.esta_na_branch({PROD: None}, PROD, "106185")),
            [MAIN])

    def test_esta_na_branch(self):
        self.assertTrue(ciclo.esta_na_branch({PROD: {"1"}}, PROD, "1"))
        self.assertFalse(ciclo.esta_na_branch({PROD: {"2"}}, PROD, "1"))
        self.assertFalse(ciclo.esta_na_branch({PROD: None}, PROD, "1"))
        self.assertFalse(ciclo.esta_na_branch({}, PROD, "1"))
        self.assertFalse(ciclo.esta_na_branch({PROD: {"1"}}, "", "1"))
        self.assertFalse(ciclo.esta_na_branch({PROD: {"1"}}, PROD, ""))


class TestAtribuicao(unittest.TestCase):
    """Quem ABRE a tarefa costuma ser o suporte; quem responde por ela é a
    pessoa atribuída. São coisas diferentes do autor do PR, que é do GitHub."""

    def test_chega_na_linha_com_pr_aberto(self):
        linhas = ciclo.montar(
            [pr(8006, MAIN, "108692", autor="quem-abriu-o-pr")],
            {"108692": tarefa("Corretiva", atribuido="Ruan Sampaio")},
            PROD, MAIN, ["Corretiva"], [], hoje=HOJE,
            historico={MAIN: set(), PROD: set()})
        linha = uma(linhas, "108692")
        self.assertEqual(linha["atribuido"], "Ruan Sampaio")
        self.assertEqual(linha["autores"], ["quem-abriu-o-pr"])

    def test_chega_na_linha_sem_pr_aberto(self):
        linhas = ciclo.montar(
            [], {"108692": tarefa("Corretiva", atribuido="Ruan Sampaio")},
            PROD, MAIN, ["Corretiva"], [], hoje=HOJE, base_homologacao=HOMO,
            historico={MAIN: {"108692"}, PROD: set(), HOMO: set()}, exigir_pr=False)
        self.assertEqual(uma(linhas, "108692")["atribuido"], "Ruan Sampaio")

    def test_tarefa_que_a_carga_nao_achou_fica_com_vazio(self):
        linhas = ciclo.montar([pr(8010, MAIN, "999999")], {}, PROD, MAIN, [], [],
                              hoje=HOJE, historico={MAIN: set(), PROD: set()})
        self.assertEqual(uma(linhas, "999999")["atribuido"], "")

    def test_lista_da_tela_nao_repete_nem_traz_vazio(self):
        linhas = [{"atribuido": "Ruan Sampaio"}, {"atribuido": "Ruan Sampaio"},
                  {"atribuido": ""}, {"atribuido": "Outra Pessoa"}, {}]
        self.assertEqual(ciclo.atribuidos_vistos(linhas),
                         ["Outra Pessoa", "Ruan Sampaio"])


class TestContratoDaLinha(unittest.TestCase):
    """A grade e a exportação leem estas chaves de toda linha, nos dois caminhos
    que produzem linha: agrupada por PR aberto e a que entra pelo build vazio."""

    CHAVES = ("pendencia", "pendente_em", "tarefa", "tipo", "status_wp", "entrega",
              "tem_entrega", "versoes", "ramos", "obrigatorias", "branches",
              "pr_principal", "pr_producao", "pr_homologacao", "pr_outros",
              "build", "idade", "assunto", "detalhe", "autores", "atribuido",
              "prs", "urls")

    def test_as_duas_origens_de_linha_tem_todas_as_chaves(self):
        linhas = ciclo.montar(
            [pr(8030, PROD, "109866")],
            {"109866": tarefa("Adaptativa", entrega=True, ramos="2602"),
             "104328": tarefa("Adaptativa", fechado=True)},
            PROD, MAIN, ["Corretiva"], [], hoje=HOJE, base_homologacao=HOMO,
            historico={MAIN: set(), PROD: set(), HOMO: set()})
        self.assertEqual(len(linhas), 2)
        for linha in linhas:
            faltando = [c for c in self.CHAVES if c not in linha]
            self.assertEqual(faltando, [], "%s: %s" % (linha["tarefa"], faltando))
            for lado in ("principal", "producao", "homologacao", "outros"):
                self.assertIsInstance(linha["urls"][lado], list)
            for branch in linha["branches"]:
                self.assertEqual(sorted(branch), ["mergeado", "nome", "obrigatoria",
                                                  "pendente", "prs", "situacao", "urls"])


class TestExcel(unittest.TestCase):
    def setUp(self):
        self.caminho = os.path.join(os.environ.get("TEMP", "."), "teste-ciclo.xlsx")

    def tearDown(self):
        if os.path.exists(self.caminho):
            os.remove(self.caminho)

    def textos(self, zip_saida, aba):
        raiz = ET.fromstring(zip_saida.read("xl/worksheets/sheet%d.xml" % aba))
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        return [no.text for no in raiz.iter(ns + "t")]

    def test_escreve_duas_abas_com_o_que_falta(self):
        excel.escrever(self.caminho, [
            ("Tarefas", ["TAREFA", "PENDENTE EM", "DIAS"],
             [["108692", "v2.2602: PR não aberto", 32]], [10, 40, 6]),
            ("Pendências por branch", list(ciclo.COLUNAS_LONGO),
             [["108692", "Corretiva", "Em teste", HOMO, "sim", "PR não aberto",
               "sim", ciclo.SEM_VERSAO, "sim", "2602", "Erro ao copiar perfil"]], None),
        ])
        with zipfile.ZipFile(self.caminho) as zip_saida:
            self.assertIsNone(zip_saida.testzip())
            self.assertIn("xl/worksheets/sheet2.xml", zip_saida.namelist())
            self.assertIn("v2.2602: PR não aberto", self.textos(zip_saida, 1))
            longo = self.textos(zip_saida, 2)
            self.assertIn(HOMO, longo)
            self.assertIn(ciclo.SEM_VERSAO, longo)

    def test_escapa_e_numero_continua_numero(self):
        excel.escrever(self.caminho, [("A", ["X & <Y>"], [["a<b", 7]], None)])
        with zipfile.ZipFile(self.caminho) as zip_saida:
            bruto = zip_saida.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("X &amp; &lt;Y&gt;", bruto)
        self.assertIn("<v>7</v>", bruto)

    def test_coluna_por_indice(self):
        self.assertEqual([excel.coluna(i) for i in (0, 1, 25, 26, 27)],
                         ["A", "B", "Z", "AA", "AB"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
