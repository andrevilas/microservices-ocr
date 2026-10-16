# Painel com histórico persistente — OCR-AM-12

O painel restaura todas as páginas de `GET /api/v1/jobs` ao abrir e atualiza a cada
cinco segundos enquanto não há ação nem detalhe aberto. Uma falha de paginação
preserva os cards anteriores. O backend aplica o dono; o navegador não decide
quais jobs pertencem ao usuário. Dados antigos de jobs em localStorage são
descartados. Apenas busca e ordenação são preferências locais, por identidade.

Os arquivos selecionados e o caminho relativo de pasta permanecem nesta aba até
o envio; o caminho não é persistido no servidor. Reabrir a página restaura jobs
admitidos, mas não arquivos locais ainda não enviados nem seleções. O volume e
a ordenação por tamanho usam apenas arquivos presentes na aba; o tamanho dos
resultados restaurados consta do relatório. A ordenação da API é fixa por
criação/ID; a ordenação visual ocorre depois de obter todas as páginas.

## Sessão e contratos

`POST /auth/browser-session` recupera o Bearer humano já existente no cookie
HttpOnly, sem renovar sua expiração. Exige `Origin` igual à origem da aplicação,
`X-OCR-Browser: 1` e não aceita `Sec-Fetch-Site: cross-site`. Não admite identidade
de serviço. A resposta usa `Cache-Control: no-store` e `Pragma: no-cache`.

O token fica em memória, nunca em localStorage, query string ou URL de download.
Todas as operações OCR usam `/api/v1` com Bearer explícito e `credentials: omit`.
A API v1 continua recusando autenticação apenas por cookie. O reverse proxy deve
preservar a origem externa e configurar corretamente os headers encaminhados;
uma divergência de origem impede o bootstrap. O logout conserva a semântica atual
da sessão humana; revogação de JWT/OAuth segue em AM-13, sem promessa de revogação
do token já copiado. Conta e administração continuam nas telas existentes (AM-14).

## Ações e recuperação

| Ação | Efeito |
|---|---|
| Enviar selecionados / modo rápido | Reserva upload, transmite PDF por PUT, admite lote idempotente com opções verificadas em capabilities |
| Retomar envio anterior | Reusa corpo e chave do pedido de admissão cuja resposta se perdeu, inclusive após mudar as opções visuais |
| Cancelar | Solicita cancelamento de um job; estado `cancel_requested` é distinto de `canceled` |
| Cancelar selecionados | Envia até 25 IDs explícitos e mostra o resultado por item; confirmação antes da chamada |
| Reprocessar | Cria outro job, com opções selecionadas e `parent_job_id`; repetição de transporte mantém a chave |
| Excluir arquivos | Confirma exclusão irreversível do job terminal; mantém histórico e informa purge_pending |
| Ocultar encerrados / card | Altera somente a visualização da aba |
| Limpar visualização | Oculta histórico e descarta arquivos locais não enviados; não cancela nem exclui jobs |
| Restaurar histórico | Mostra novamente os jobs do backend |
| Texto / relatório | Conteúdo paginado e JSON do mesmo contrato consumido por MCP |
| PDF / ZIP | Download autenticado como binário, sem token na URL; ZIP até 25 resultados não ocultos |

Enquanto uma admissão é incerta, limpar a visualização fica desabilitado e o
botão de envio permite retomá-la. Recarregar a página descarta essa chave em
memória: antes de enviar novamente, consulte o histórico restaurado. O mesmo
pedido de ZIP é consultado novamente após timeout; pedidos expirados permitem
uma nova tentativa. Seleção de pasta não concede leitura irrestrita ao servidor.

## Verificação reproduzível

Instale dependências de desenvolvimento e Chromium (`python -m playwright
install chromium`) ou defina `OCR_TEST_BROWSER` com o executável do navegador.
Execute `python -m pytest tests/test_workspace_ui.py`. No Windows o teste usa
Chrome instalado por padrão. `OCR_SKIP_BROWSER_TESTS=1` permite pular somente
quando não há navegador; não foi usado na homologação local. `OCR_UI_EVIDENCE_DIR`
salva imagens desktop/mobile. Os testes criam API, banco, credenciais e PDFs
sintéticos em diretório temporário e encerram apenas seus processos.

Cobertura: sessão humana/same-origin, Bearer obrigatório, upload/resultados,
recarga, 103 registros em mais de uma página, isolamento por dono, descarte do
cache antigo, texto por página, PDF, ZIP, retry, cancelamento, exclusão e reset
sem mutação. Perda de resposta após admissão confirma repetição sem duplicação.
PDF inválido não bloqueia a limpeza visual. Casos de XSS executados no navegador
tratam nomes, caminhos, erros, texto e relatório como dados, e impedem execução
de uma URL de download injetada.
