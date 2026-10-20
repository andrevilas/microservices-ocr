# Fila persistente de entrega — implementação local

O schema aditivo `rag_outbox` e a auditoria mínima são criados no banco
do JobStore, sem modificar jobs legados ou presumir entregas anteriores.
Não há worker, rota ou ferramenta MCP ativada no startup. O componente
`RagDeliveryWorker` está disponível para inicialização explícita, conforme
[rag-worker.md](rag-worker.md).

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
A consulta/execução/admissão e o ciclo do worker reconciliam prazos vencidos.
O snapshot do worker fornece alertas agregados locais; não há envio
automático de notificações nem interface pública de monitoramento.

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

`recovery_package` permite ao proprietário com `jobs:read` e
`artifacts:read` recuperar os bytes imutáveis do pacote pendente, inclusive
após o vencimento normal dos artefatos. A operação não libera o hold nem
confirma armazenamento externo. Após cancelamento ou recibo indexed, a
cópia da fila já removida não pode ser recuperada por essa operação.
`receipt` exige proprietário e `jobs:read`, devolve o último recibo validado
e permanece disponível após reinício e liberação da cópia temporária.
Estas operações de serviço ainda precisam de transporte API/MCP.

## Limites e pendências antes de produção

O limite atual mede quantidade de registros e bytes do pacote na fila.
Não representa quota global de originais, derivados, metadados, SQLite/WAL
e backups. Registros concluídos continuam contando para a capacidade:
faltam política de arquivo/expiração de recibos e rotina de manutenção.
Essa escolha limita crescimento e pode bloquear novas entregas.

O estado resolution evita perda silenciosa, mas não é resolução automática
do acervo vencido. Antes de ativar, definir política operacional aprovada
de exportação/cancelamento, monitoramento e alertas, quota total de disco,
valores de prazo, integração do worker ao runtime e API/MCP. A troca de
credenciais em memória já é suportada pelo worker. Não declarar
retenção temporal plenamente resolvida enquanto essas peças faltarem.

Também pendentes: revisão independente do código, revalidação do receptor
e homologação de implantação/rollback. Páginas vazias já são preservadas
nos candidatos locais do adaptador e do receptor, com testes de numeração.
Nenhum acervo real foi migrado.

## Evidências exigidas

Testes de reinício, replay, concorrência envio/limpeza/cancelamento, resposta
tardia, quotas, propriedade, vencimento, ACK inválido/pending e falha de
commit antes/depois do efeito. Ensaio real com PDF sintético processado pelo
OCR, servidor RAG separado, consulta com página e recuperação do mesmo PDF.
