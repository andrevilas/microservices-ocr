# Worker e monitor local da entrega RAG

`RagDeliveryWorker` não inicia threads no import nem no construtor.
`start()` é explícito; o runtime candidato chama-o somente com `RAG_CONFIG_FILE`
configurado. Consulte [API/MCP e ativação](rag-api-mcp.md).
O operador fornece destinos, intervalo, tamanho do lote e antecedência
de aviso. Não há valores predefinidos de produção neste incremento.

## Operação

- `collect()` processa no máximo o tamanho configurado do lote (1–100).
  Ciclos simultâneos da mesma instância retornam busy. As reservas duráveis
  da outbox continuam coordenando as tentativas e a recuperação após queda.
- Somente itens prontos, sem reserva de execução vigente, são elegíveis.
  O worker não ignora backoff nem o prazo máximo de entrega.
- A configuração é associada à identidade completa registrada na outbox,
  incluindo proprietário, destino e classificação. Não há fallback para
  outro endereço ou token. Configuração ausente exige resolução e preserva
  os arquivos. Restaurar configuração não retoma automaticamente um item
  já marcado para resolução.
- `replace_destinations()` valida e substitui o cadastro em memória.
  Permite rotação do token sem alterar a associação; uma requisição já
  iniciada pode concluir usando a credencial anterior.
- Falha inesperada interrompe a thread, registra somente `worker_failed`
  em memória e conserva a reserva durável para recuperação. Não escreve
  exceções, documentos, tokens ou respostas remotas nos logs.
- `stop(timeout)` devolve falso se ainda houver tentativa em execução.
  A instância não inicia outra thread enquanto a anterior estiver viva.
  Parar o worker não cancela uma entrega já transmitida ao destino.

O limite é de quantidade de tentativas por ciclo, não um prazo absoluto
para encerrar I/O. O transporte continua sujeito aos timeouts existentes;
o shutdown deve respeitar um resultado falso de `stop()`.

## Monitoramento

`snapshot()` reconcilia vencimentos e retorna contagens por estado,
quantidade pronta, próxima do vencimento e exigindo resolução, bytes
dos pacotes ainda na fila e falha local do worker. Os avisos são calculados
a partir dos prazos, sem renová-los. Não contém IDs de jobs, texto, URL,
proprietários ou tokens.

Este método é interno ao operador e agrega todos os proprietários.
Não expor diretamente como API/MCP de usuário sem autorização própria.
Não existe envio de notificações por e-mail, WhatsApp ou outro canal.

`observe()` acrescenta pressão de disco e emite avisos agregados no log
somente quando muda a combinação de resolução pendente, prazo próximo e
espaço abaixo da margem. A limpeza desses sinais emite uma mensagem informativa.
O ciclo periódico chama essa observação; falha inesperada encerra o worker
com aviso fixo, sem conteúdo da exceção. Logs precisam ser coletados pelo operador;
isso não comprova entrega de alerta a uma pessoa ou sistema externo.

## Validação e pendências

Testes cobrem ausência de início implícito, limite do lote, replay,
backoff, troca de token, configuração ausente, prazo vencido, monitor sem
segredos, ciclos concorrentes, parada durante HTTP e queda inesperada.
O ensaio com servidor RAG real também executa `collect()` sobre a fila de
um PDF sintético processado pelo OCR e verifica o estado indexed.

O candidato já integra lifespan/API/MCP. Continuam pendentes revisão independente,
qualificação da política de resolução/exportação, coleta operacional dos avisos
e arquivo de recibos. O controle persistente TrueNAS está descrito em
[armazenamento RAG](rag-storage-operations.md); temporários fora desse volume
precisam ser considerados no release. A nova aplicação ainda não foi publicada.
