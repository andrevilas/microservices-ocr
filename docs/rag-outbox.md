# Fila persistente de entrega — implementação local

O schema aditivo `rag_outbox` e a auditoria mínima são criados no banco
do JobStore, sem modificar jobs legados ou presumir entregas anteriores.
Não há worker automático, rota ou ferramenta MCP ativada neste incremento.

`RagOutbox` recebe uma `DeliveryPolicy` explícita, sem valores de produção
predefinidos: quantidade máxima de registros, bytes dos pacotes, prazo do
hold, retenção após recibo, limite de tentativas, backoff e duração da
reserva de execução. Os números nos testes são exclusivamente sintéticos.

## Proteção e estados

Admissão mantém um lease de leitura desde a preparação até a persistência.
O pacote imutável, a associação de proprietário/destino e o hold entram
na mesma transação SQLite. Não são persistidos tokens. A limpeza, exclusão
explícita e expurgo consultam o hold sob o lock compartilhado do JobStore.
O runtime existente deve continuar tendo somente um JobStore ativo; isto
não implementa coordenação de múltiplos processos do OCR.

Estados: queued, sending, retry, indexed, resolution, canceled.
Uma reserva de execução persistente impede tentativas simultâneas enquanto
válida. Após queda do worker, a mesma entrega pode ser retomada quando a
reserva vencer; o destino deve manter idempotência. Respostas tardias não
concluem uma tentativa substituída ou vencida.

Timeout mantém a incerteza e o hold. Recibo pending/failed não libera arquivos.
Recibo inválido, rejeição definitiva ou limite de tentativas exige resolução.
A consulta/execução/admissão reconcilia prazos vencidos; não há monitor
automático de alertas neste incremento.

Após recibo indexed correspondente, persistem-se recibo, estado e prazo
pós-recebimento na mesma transação. O prazo nunca encurta a retenção já
existente. O pacote duplicado sai da fila e o hold é liberado. O PDF não
é apagado nesse momento. Falha ambígua no commit coloca o JobStore em
quarentena; é necessário reiniciar e reconciliar a partir do banco.

Vencimento exige resolução e bloqueia novas admissões, sem renovar o prazo
nem descartar silenciosamente arquivos. O hold permanece até resolução.
Cancelamento exige proprietário e `artifacts:delete`, é auditado, não mata
uma solicitação remota já transmitida e permite novamente a retenção
legada; arquivos vencidos podem ser removidos pelo próximo coletor.
Não expor cancelamento como ação implícita da interface.

## Limites e pendências antes de produção

O limite atual mede quantidade de registros e bytes do pacote na fila.
Não representa quota global de originais, derivados, metadados, SQLite/WAL
e backups. Registros concluídos continuam contando para a capacidade:
faltam política de arquivo/expiração de recibos e rotina de manutenção.
Essa escolha limita crescimento e pode bloquear novas entregas.

O estado resolution evita perda silenciosa, mas não é resolução automática
do acervo vencido. Antes de ativar, definir política operacional aprovada
de exportação/cancelamento, monitoramento e alertas, quota total de disco,
valores de prazo, rotação de credenciais, worker e API/MCP. Não declarar
retenção temporal plenamente resolvida enquanto essas peças faltarem.

Também pendentes: revisão independente do código, revalidação do receptor,
aceitação de páginas vazias no RAG e homologação de implantação/rollback.
Nenhum acervo real foi migrado.

## Evidências exigidas

Testes de reinício, replay, concorrência envio/limpeza/cancelamento, resposta
tardia, quotas, propriedade, vencimento, ACK inválido/pending e falha de
commit antes/depois do efeito. Ensaio real com PDF sintético processado pelo
OCR, servidor RAG separado, consulta com página e recuperação do mesmo PDF.
