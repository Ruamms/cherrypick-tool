# Ferramentas de backport

Duas ferramentas de janela, para quem mantém uma **branch de produção** separada da branch
principal e precisa levar correções de uma para a outra.

O problema que elas resolvem: você mandou duas correções para a `master`, portou só uma para
a branch de produção e descobriu a que faltou pelo cliente reclamando.

| Ferramenta | Para quê |
| ---------- | -------- |
| **BackportCheck** | **Descobrir** o que está na principal e ainda não chegou na produção — e portar dali mesmo. |
| **CherryPickPush** | **Portar** quando você já sabe o commit e o nome da branch. |

Ambas são git puro: sem API, sem token, sem servidor.

## Baixar

Não precisa de Python nem saber compilar — baixe o `.exe` e execute:

- **[BackportCheck.exe](../../raw/main/dist/BackportCheck.exe)** (~11 MB)
- **[CherryPickPush.exe](../../raw/main/dist/CherryPickPush.exe)** (~11 MB)

> Os executáveis não são assinados digitalmente. Na primeira execução o Windows mostra
> *"O Windows protegeu o seu PC"* — clique em **Mais informações → Executar assim mesmo**.
> Se preferir não confiar num binário, o código-fonte são dois arquivos `.py` nesta pasta;
> a seção [Build](#build) mostra como gerar o exe você mesmo.

## Pré-requisitos

- Windows 64-bit;
- `git` no PATH (`git --version` tem que responder). A coluna **CONFLITO** usa
  `git merge-tree --write-tree`, que existe a partir do **git 2.38**; em versões mais
  antigas a coluna mostra `?` e o resto funciona normalmente;
- um clone do repositório, com remote `origin` e o working tree limpo na hora de portar.

O executável embute o runtime do Python — **não** é preciso ter Python instalado.

---

# BackportCheck

Responde "o que eu mandei para a principal e esqueci de mandar para a produção?".
Compara `origin/<principal>` com `origin/<produção>` e lista o que falta portar.

## Uso

| Campo | Exemplo | Observação |
| ----- | ------- | ---------- |
| Repositorio | `C:\repos\meu-projeto` | qualquer repo git com remote `origin` |
| Branch de producao | `release-2601` | troque quando virar a versão |
| Branch principal | `master` | ou `main` |
| Autor | `(todos)` | lista preenchida pela análise; escolha o seu nome para ver só o seu |
| Dias | `180` | janela de commits da principal |

Clique em **Analisar**. Trocar o autor depois **não** reprocessa o git — o filtro é aplicado
sobre o resultado já em memória.

Tudo fica salvo em `%APPDATA%\cherrypick-tool\backport.json`.

## As quatro situações

| Situação | Significa | Detectado por |
| -------- | --------- | ------------- |
| **PENDENTE** (vermelho) | nenhum sinal de backport | é o caso que queima cliente |
| **BRANCH CRIADA** (laranja) | a branch de backport existe no `origin`, mas nada chegou na produção | PR de backport aberto ou abandonado |
| **PROVAVEL** (cinza) | o número da tarefa já aparece na produção, com outro assunto | típico de PR de backport intitulado com o nome da branch |
| *(não listado)* | já portado | patch-id equivalente (`git log --cherry-pick`) ou assunto normalizado igual |

O número da tarefa é qualquer número de 5 a 7 dígitos no assunto do commit
(`Corretiva - tarefa 123456`). A normalização do assunto ignora acento, pontuação e o
`(#1234)` do squash merge — o PR do backport tem número diferente do PR original, então
ele não pode entrar na comparação.

O histórico de produção é lido com janela de 3 anos, independente do campo **Dias**:
um backport pode ter sido feito muito depois do commit original.

**PROVAVEL não é prova.** Duas correções da mesma tarefa, com PRs diferentes, caem aí — e
só uma pode ter ido. Confira antes de descartar.

## Coluna CONFLITO

Cada linha é testada com um cherry-pick **simulado** sobre a ponta da produção:

| Valor | Significa |
| ----- | --------- |
| `limpo` | aplica sem conflito — dá para portar agora |
| `conflito (N arq.)` | vai parar para você resolver na mão; selecione a linha e a barra de status mostra quais arquivos |
| `...` | ainda checando |
| `?` | não deu para simular (commit raiz, por exemplo) |

A simulação usa `git merge-tree`, que faz o merge de três vias **em memória**: nada é
alterado no seu working tree, no índice ou em qualquer branch. Roda em segundo plano,
preenchendo linha a linha, sem travar a janela.

A checagem é de cada commit **isolado** contra a produção. Ao levar vários commits de uma
vez, o segundo é aplicado depois do primeiro, então o resultado real pode ser melhor que o
mostrado.

## Cherry-pick + push pelo botão

Selecione uma linha (ou **várias**, para levar tudo na mesma branch — o caso de uma tarefa
com dois PRs na principal) e clique em **Cherry-pick + push do selecionado**:

1. sugere o nome da branch, editável na hora: `fb_<tarefa>_<sufixo>`, sufixo = último grupo
   de dígitos da branch de produção (`release-2601` → `fb_123456_2601`);
2. avisa se algum commit selecionado não é PENDENTE, ou se algum vai dar conflito;
3. cria a branch a partir de `origin/<produção>`, faz cherry-pick na ordem cronológica,
   dá push e abre a página de criação do PR no navegador;
4. re-analisa a lista no fim.

Em conflito ele **para** e não empurra nada — mesmas travas do CherryPickPush.

> Se a sua branch principal usa squash merge, o nome da branch de origem do PR não existe
> no histórico: sobram o assunto e o `(#1234)`. Por isso o padrão é `fb_<tarefa>_<sufixo>`,
> e não `<branch origem>_<sufixo>`.

Duplo clique numa linha abre o PR de origem no navegador (ou o commit, se o assunto não
trouxer o `(#1234)`).

---

# CherryPickPush

Cria a branch final a partir de `origin/<branch base>`, faz cherry-pick dos commits
informados e publica sob confirmação.

## Uso

Preencha os 4 campos e clique em **Preparar**:

| Campo | Exemplo |
| ----- | ------- |
| Repositorio | `C:\repos\meu-projeto` |
| Branch base | `release-2601` |
| Commits | `9f8e7d6c5b4a39281706f5e4d3c2b1a098765432` |
| Branch final | `fb_123456_2601` |

Vários commits: um por linha, aplicados na ordem digitada.

O botão **Push** só habilita depois de um cherry-pick limpo, e revalida o estado antes de
enviar. Se o push der certo e o checkbox **Abrir a página do PR no navegador após o push**
estiver marcado (padrão), abre `<origin>/compare/<base>...<final>?expand=1` no navegador —
só a página de criação; o PR nasce quando você confirmar lá. A URL também vai para o log.

Botão **Abortar cherry-pick** = `git cherry-pick --abort` (não apaga a branch criada).

## O que ele faz

```
git fetch origin --prune
git checkout --no-track -b <final> origin/<base>
git cherry-pick <commit>...
git push -u origin <final>        # só no clique do botão Push
```

A URL web vem de `git remote get-url origin` — aceita `https://` e `git@host:org/repo`.

---

## Travas

- Aborta se o working tree estiver sujo.
- Aborta se já houver cherry-pick/merge em andamento.
- Aborta se a branch final já existir local ou no `origin`.
- Aborta se a base ou algum commit não existir depois do fetch.
- Em conflito: **para**, lista os arquivos e deixa o estado para resolver na mão. Nunca faz
  `--continue` sozinho, nunca empurra.
- `--no-track` na criação da branch, para o upstream não nascer apontando para a base.

## O que elas não fazem

- Não usam a API do GitHub; não pedem, não guardam e não leem token nenhum. O push sai pelo
  seu `git`, com as suas credenciais — o PR aparece no seu nome.
- Não fazem `push --force`, não apagam branch e não reescrevem histórico.
- Não alteram nada no repositório para checar conflito.
- Não criam o PR sozinhas: abrem a página de criação e param aí.

## Build

Python 3.11 e PyInstaller 6.22:

```
python -m PyInstaller --noconfirm --onefile --noconsole --name BackportCheck backport_check.py
```

```
python -m PyInstaller --noconfirm --onefile --noconsole --name CherryPickPush cherrypick_tool.py
```

`backport_check.py` importa o núcleo git de `cherrypick_tool.py`; os dois arquivos precisam
estar na mesma pasta. Os últimos valores digitados ficam em `%APPDATA%\cherrypick-tool\`
(`backport.json` e `last.json`).

## Licença

MIT — veja [LICENSE](LICENSE).
