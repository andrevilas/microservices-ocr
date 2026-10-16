# Painel de documentos — OCR-AM-12 e revisão de interface

O painel restaura todas as páginas de `GET /api/v1/jobs` ao abrir e repete essa
sincronização completa a cada 60 segundos e ao clicar em **Atualizar**. Voltar à
aba retoma o ciclo, com sincronização completa se o intervalo já venceu.
Entre as sincronizações completas, a cada 5 segundos consulta os
jobs em andamento (`status=queued|processing|cancel_requested` e `GET /jobs/{id}`
para os que deixaram esses estados) e as métricas enquanto há jobs ativos.
O polling pausa com a aba oculta. Uma falha
de paginação preserva a lista anterior e aparece como "Falha ao atualizar" ao lado
do horário da última sincronização. O backend aplica o dono; o navegador não decide
quais jobs pertencem ao usuário. Dados antigos de jobs em localStorage são
descartados. Apenas busca e ordenação são preferências locais, por identidade.

Os arquivos escolhidos ficam numa lista de preparação, separada do histórico, até
serem admitidos. O caminho relativo de pasta permanece nesta aba e não é persistido
no servidor. Reabrir a página restaura jobs admitidos, mas não arquivos locais ainda
não enviados nem seleções. Tamanho e caminho aparecem apenas para arquivos enviados
nesta aba; o tamanho de resultados restaurados consta do relatório. A ordenação da
API é fixa por criação/ID; ordenação e filtros visuais (estado, busca, período,
qualidade e lote) ocorrem depois de obter todas as páginas. O filtro de lote usa
`GET /api/v1/batches` e o manifesto de cada lote. A lista mostra 50 documentos por
vez, com "Mostrar mais".

A interface é renderizada por chave: cada documento mantém o mesmo elemento entre
atualizações, de modo que o foco do teclado, menus abertos e seleção sobrevivem ao
polling. Detalhes abrem num painel lateral (tela cheia no celular) que recebe o
foco, fecha com Esc e devolve o foco ao botão de origem; menus fecham com Esc.
No celular o detalhe funciona como diálogo modal: o conteúdo encoberto fica
inativo e os erros da operação aparecem dentro do próprio painel.
Avisos ficam visíveis até serem fechados ou substituídos pelo resultado da próxima
ação; erros usam `role="alert"`. Mensagens da API são exibidas em português pelo
código (`error.code`), mantendo o código entre parênteses.

## Sessão e contratos

`POST /auth/browser-session` recupera o Bearer humano já existente no cookie
HttpOnly, sem renovar sua expiração. Exige `Origin` igual à origem da aplicação,
`X-OCR-Browser: 1` e não aceita `Sec-Fetch-Site: cross-site`. Não admite identidade
de serviço. A resposta usa `Cache-Control: no-store` e `Pragma: no-cache`.

O token fica em memória, nunca em localStorage, query string ou URL de download.
Todas as operações OCR usam `/api/v1` com Bearer explícito e `credentials: omit`;
caminhos são normalizados e precisam permanecer em `/api/v1/` da mesma origem.
A API v1 continua recusando autenticação apenas por cookie. O reverse proxy deve
preservar a origem externa e configurar corretamente os headers encaminhados;
uma divergência de origem impede o bootstrap. O logout conserva a semântica atual
da sessão humana; revogação de JWT/OAuth segue em AM-13, sem promessa de revogação
do token já copiado. Conta e usuários continuam nos contratos legados (AM-14).

## Ações e recuperação

| Ação | Onde | Efeito |
|---|---|---|
| Escolher PDFs / pasta / arrastar | Painel "Enviar PDFs" | Adiciona à lista de preparação; arquivos que não são PDF são ignorados e contados; acima do limite ficam marcados como rejeitados |
| Enviar N PDFs | Painel "Enviar PDFs" | Reserva upload, transmite por PUT e admite lote idempotente com as opções verificadas em capabilities. Envia no máximo `min(batch_max_items, pending_jobs_per_owner − em andamento)`; o restante continua na lista |
| Enviar um único PDF agora | Painel "Enviar PDFs" | Mesmo fluxo para um arquivo, sem revisar a lista. Com um envio pendente, o arquivo entra na lista e nada é reenviado |
| Retomar envio anterior | Botão principal após falha ambígua | Repete corpo e chave da admissão cuja resposta se perdeu, mesmo que as opções visuais mudem |
| Descartar envio pendente | Abaixo do botão principal | Confirmação explícita avisando que reenviar pode duplicar jobs; atualiza o histórico para conferência |
| Cancelar | Linha, detalhe | Confirmação; `cancel_requested` é distinto de `canceled` |
| Cancelar selecionados | Barra de seleção | Até `cancel_max_items` IDs explícitos, confirmação e resumo por resultado |
| Reprocessar com as mesmas opções | Menu da linha, detalhe | `POST retry` com corpo `{}`; herda as opções originais. Jobs legados sem opções usam as opções atuais |
| Reprocessar com as opções atuais | Menu da linha, detalhe | `POST retry` com as opções do painel |
| Excluir arquivos | Menu da linha, zona de exclusão do detalhe | Confirmação explicando que original e resultado saem e que reprocessar deixa de ser possível; histórico permanece; informa `purge_pending` |
| Ocultar desta lista / Ocultar encerrados / Mostrar ocultos | Menu da linha, menu Visualização | Somente a aba |
| Limpar visualização | Menu Visualização | Confirmação; oculta o histórico e esvazia a lista de preparação; não cancela nem exclui. Desabilitado com envio pendente |
| Texto por página | Detalhe | Seletor de página, página anterior, "Próximo trecho", copiar trecho |
| Relatório | Detalhe | Resumo legível (qualidade heurística, origem por página, validação, checksums, motores, avisos) e JSON técnico integral |
| Baixar PDF | Linha, detalhe | Download autenticado como binário, sem token na URL |
| Baixar ZIP | Barra de seleção | ZIP dos selecionados concluídos e disponíveis (até `export_max_items`); informa itens não incluídos e validade |

Classificação de falhas para pedidos com chave de idempotência (lote, retry, ZIP):
400, 403, 404, 409, 410, 413, 415, 422 e 429 são definitivos — o servidor não
persistiu nada (`job_quota_exceeded`, `export_queue_full` e `export_quota_exceeded`
são verificados antes da gravação) — e liberam um novo pedido. Falha de rede, 408 e
5xx são ambíguos e mantêm a mesma chave e corpo. Antes do POST de admissão, falhas
de upload nunca deixam pedido pendente; uploads já validados são reutilizados. Um
arquivo rejeitado pela validação (400, 409, 413, 415, 422) fica marcado na lista e
os demais seguem no mesmo envio. Recarregar a página descarta chaves em memória:
consulte o histórico restaurado antes de reenviar. Seleção de pasta não concede
leitura irrestrita ao servidor.

## Verificação reproduzível

Instale dependências de desenvolvimento e Chromium (`python -m playwright
install chromium`) ou defina `OCR_TEST_BROWSER` com o executável do navegador.
Execute `python -m pytest tests/test_workspace_ui.py tests/test_ui_design.py`. No
Windows o teste usa Chrome instalado por padrão. `OCR_SKIP_BROWSER_TESTS=1` permite
pular somente quando não há navegador. `OCR_UI_EVIDENCE_DIR` salva imagens
desktop/mobile do painel, login, conta, usuários e integrações (nunca com segredo
na tela). Os testes criam API, banco, credenciais e PDFs sintéticos em diretório
temporário e encerram apenas seus processos.

Cobertura: sessão humana/same-origin, Bearer obrigatório, upload/resultados,
recarga, 103 registros em mais de uma página, isolamento por dono, descarte do
cache antigo, texto por página, PDF, ZIP por seleção, retry, cancelamento com
confirmação, exclusão e reset sem mutação. Perda de resposta após admissão confirma
repetição sem duplicação; rejeição por cota libera nova chave e reutiliza o upload;
ZIP e retry mantêm a chave em falha ambígua e a liberam em falha definitiva. Foco
e seleção sobrevivem ao polling; detalhe e menus funcionam pelo teclado. Falha no
histórico não bloqueia o envio. Casos de XSS executados no navegador tratam nomes,
caminhos, erros, texto e relatório como dados, e impedem execução de uma URL de
download injetada. A tela de integrações exibe o segredo uma única vez e o remove
do DOM ao concluir. Revisão de design e pendências: [ui-review.md](ui-review.md).
