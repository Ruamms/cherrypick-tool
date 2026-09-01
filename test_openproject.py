"""Testes do cliente do OpenProject. Rode com: python -m unittest -v

O caso que motivou: o filtro `id` da API v3 valida cada valor contra os work
packages VISIVEIS ao usuario, e um unico numero invisivel reprova o lote inteiro
com 400 - derrubando a carga da aba Ciclo para quem nao ve todos os projetos.
"""

import json
import unittest

import openproject
from cherrypick_tool import StepError


def wp(numero):
    return {"_type": "WorkPackage", "id": int(numero)}


class ApiPorLista(openproject.OpenProject):
    """OpenProject com `_get` de mentira: so os ids em `visiveis` existem."""

    def __init__(self, visiveis):
        openproject.OpenProject.__init__(self, "https://op.exemplo", "tok")
        self.visiveis = {str(i) for i in visiveis}
        self.lotes = []

    def _get(self, caminho):
        from urllib.parse import parse_qs, urlparse
        filtros = json.loads(parse_qs(urlparse(caminho).query)["filters"][0])
        lote = filtros[0]["id"]["values"]
        self.lotes.append(list(lote))
        if any(i not in self.visiveis for i in lote):
            erro = StepError("OpenProject respondeu 400 (Id Values ...)")
            erro.codigo_http = 400
            raise erro
        return {"_embedded": {"elements": [wp(i) for i in lote]}}


class FiltroIdPorVisibilidade(unittest.TestCase):

    def test_lote_todo_visivel_faz_uma_chamada(self):
        api = ApiPorLista(range(100, 110))
        achados, ignorados = api.work_packages_por_id([100, 101, 102])
        self.assertEqual([w["id"] for w in achados], [100, 101, 102])
        self.assertEqual(ignorados, [])
        self.assertEqual(len(api.lotes), 1)

    def test_um_id_invisivel_nao_derruba_o_resto(self):
        api = ApiPorLista([100, 101, 102, 104])
        achados, ignorados = api.work_packages_por_id([100, 101, 102, 103, 104])
        self.assertEqual(sorted(w["id"] for w in achados), [100, 101, 102, 104])
        self.assertEqual(ignorados, ["103"])

    def test_varios_invisiveis_saem_todos_na_lista(self):
        api = ApiPorLista([100, 102, 104])
        achados, ignorados = api.work_packages_por_id([100, 101, 102, 103, 104, 105])
        self.assertEqual(sorted(w["id"] for w in achados), [100, 102, 104])
        self.assertEqual(ignorados, ["101", "103", "105"])

    def test_lote_inteiro_invisivel_devolve_vazio_sem_erro(self):
        api = ApiPorLista([])
        achados, ignorados = api.work_packages_por_id([100, 101])
        self.assertEqual(achados, [])
        self.assertEqual(ignorados, ["100", "101"])

    def test_erro_que_nao_e_400_continua_estourando(self):
        class ApiQueCai(openproject.OpenProject):
            def _get(self, caminho):
                erro = StepError("Token recusado (401).")
                raise erro
        api = ApiQueCai("https://op.exemplo", "tok")
        with self.assertRaises(StepError):
            api.work_packages_por_id([100, 101])

    def test_ids_vazios_nao_chamam_a_api(self):
        api = ApiPorLista([100])
        achados, ignorados = api.work_packages_por_id(["", "  "])
        self.assertEqual((achados, ignorados), ([], []))
        self.assertEqual(api.lotes, [])

    def test_divide_em_lotes_do_tamanho_pedido(self):
        api = ApiPorLista(range(100, 106))
        api.work_packages_por_id(range(100, 106), tamanho_lote=2)
        self.assertEqual(api.lotes, [["100", "101"], ["102", "103"], ["104", "105"]])


class MensagemDoErro(unittest.TestCase):

    def test_le_o_campo_message_do_corpo(self):
        class Corpo(object):
            def read(self):
                return json.dumps({"message": "Id Values nao permitido"}).encode("utf-8")
        self.assertEqual(openproject._mensagem_do_erro(Corpo()),
                         "Id Values nao permitido")

    def test_corpo_ilegivel_nao_estoura(self):
        class Corpo(object):
            def read(self):
                return b"<html>oops"
        self.assertEqual(openproject._mensagem_do_erro(Corpo()), "")


if __name__ == "__main__":
    unittest.main()
