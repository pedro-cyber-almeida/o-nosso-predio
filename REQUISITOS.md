# O Nosso Prédio — requisitos

Atualizado em 2026-10-10. As decisões abaixo substituem instruções anteriores
incompatíveis (por exemplo, a "primeira versão sem autenticação").

## Objetivo e limites

Aplicação local para um condomínio português (com o número de frações que o
condomínio tiver), usada por um administrador e pelos condóminos.

- Flask e SQLite, sem frameworks, filas ou serviços externos adicionais sem
  necessidade concreta.
- A aplicação funciona sem Gmail configurado e sem qualquer serviço externo.
- O protótipo serve em 127.0.0.1; nada publicado na Internet sem decisão
  explícita do administrador.
- Não alterar a base em uso durante desenvolvimento e testes; bases temporárias
  para ensaios. As migrações da base acontecem só quando a aplicação for
  arrancada contra ela, com o administrador presente e cópia guardada antes.

Os valores registados na aplicação refletem decisões de assembleia do condomínio.
Nada é inventado pela aplicação: uma instalação nova começa sem frações, quotas
ou movimentos.

## Frações

Frações e permilagens são criadas e geridas na interface pelo administrador, a
partir dos documentos do condomínio (ata, título constitutivo). A soma das
permilagens é validada: durante a configuração pode estar incompleta (a aplicação
mostra quanto falta e nunca a completa automaticamente); nunca pode exceder
1.000,00.

- Permitir contactos opcionais e histórico dos responsáveis.
- Não inventar andares, números de apartamento ou contactos.
- Não eliminar frações com histórico financeiro.
- Não transferir automaticamente dívidas entre proprietários.

## Utilizadores e administração

Autenticação com dois perfis. Sem registo público.

Administrador:
- gere utilizadores e associações às frações;
- configura quotas e regras;
- gere pagamentos, despesas, documentos e integrações;
- consulta o histórico de alterações.

Condómino:
- consulta apenas as frações a que está associado;
- vê cobranças, pagamentos, crédito, dívida e próximos vencimentos;
- envia comprovativos para confirmação (a partir da entrega dos documentos);
- consulta documentos explicitamente partilhados;
- altera a sua palavra-passe.

Invariantes:
- um utilizador pode estar associado a várias frações e uma fração pode ter
  vários utilizadores;
- rotas e anexos protegidos no servidor; não basta esconder botões;
- os condóminos não acedem ao Gmail, credenciais, extratos completos nem
  informação privada de outras frações;
- criação protegida do primeiro administrador, sem palavra-passe predefinida;
- não inventar identidades nem associações;
- utilizadores não são apagados: são desativados; o rasto de correções
  conserva a identidade de quem alterou.

## Quotas e regras

As regras definem-se na interface. A ata é uma origem opcional de dados, não
uma regra fixa no código.

Gerir quotas ordinárias, reforços do fundo e quotas extraordinárias, com
descrição, valor, vigência, vencimentos e frações abrangidas.

Para uma quota extraordinária:
- repartir um total igualmente;
- repartir proporcionalmente às permilagens selecionadas;
- indicar manualmente o valor de cada fração;
- dividir em prestações com datas e valores definidos.

- Pré-visualização antes de emitir.
- Arredondamentos em cêntimos conservam exatamente o total.
- Emissões duplicadas impedidas.
- Alterar uma regra não modifica silenciosamente cobranças anteriores.
- Correções a cobranças emitidas têm motivo, histórico e efeitos explícitos,
  resolvendo conflitos com pagamentos já afetados.

### Invariantes

- Regras com datas de vigência; alterações futuras não reescrevem cobranças.
- Rubricas separadas por regra; gerar o mesmo mês novamente não duplica.
- Data de vencimento configurável; sem vencimento, não classificar como vencida.
- Valores emitidos não são dinheiro recebido. Cêntimos inteiros.

## Pagamentos

- Pagamento parcial, vários pagamentos da mesma cobrança, pagamento de vários
  meses, divisão entre rubricas, adiantamento como crédito não afetado.
- Pagamento e afetações guardados separadamente.
- Invariantes: valor estritamente positivo; afetações não excedem o pagamento
  nem a dívida; crédito não afetado não liquida automaticamente; distribuir
  exige confirmação; dinheiro registado uma única vez; afetar crédito não cria
  segunda receita; anular reverte efeitos conservando histórico.
- Edição com motivo obrigatório, pré-visualização, histórico em tabela de
  correções e identidade de quem corrigiu.
- Campos de distribuição ausentes num pedido não retiram afetações: a página é
  recarregada e nada é alterado. Retirar toda a distribuição exige confirmação
  explícita nesse formulário.

## Despesas editáveis

Despesa: descrição, fornecedor, categoria, referência, data, vencimento
opcional, valor, origem dos fundos, notas e documentos associados.

- Despesa separada do pagamento; pagamentos parciais; valor em falta consultado;
  anulação com motivo.
- Editar campos de negócio de despesas e pagamentos de despesas, conservando
  histórico e recalculando efeitos (abordagem igual à dos pagamentos de
  condóminos).
- Não duplicar movimentos por reenvio; não pagar acima do valor em falta; não
  apagar histórico financeiro.
- Despesa pendente não reduz caixa; despesa paga reduz uma única vez.

## Dashboard

Administrador:
- recebimentos; despesas pagas e por pagar (quando existirem);
- quotas por receber e dívida vencida; créditos dos condóminos;
- liquidez registada; situação do fundo de reserva quando houver dados.

Gráficos com seleção de período:
- recebimentos e pagamentos de despesas por mês;
- despesas por categoria, identificando a base usada;
- evolução da liquidez.

Condómino:
- próximos vencimentos; valor por pagar, dívida vencida e crédito;
- pagamentos recentes; comprovativos pendentes (quando existirem);
- documentos partilhados (quando existirem).

Invariantes:
- cobranças distinguem-se de recebimentos; despesas de saídas efetivas;
- distribuir crédito não cria nova receita;
- o fundo de reserva não se soma novamente à liquidez total;
- sem saldos de abertura confirmados: "Por confirmar" e variação dos movimentos
  conhecidos; o saldo registado não é apresentado como saldo bancário
  reconciliado;
- nenhum número fictício nem gráfico com dados de demonstração na base real.

## Interface e design

- Design moderno, consistente e fácil de usar; boa tipografia, espaçamento,
  formulários e tabelas; adaptação a computador e telemóvel.
- Navegação clara entre dashboard, frações, quotas, pagamentos, despesas,
  documentos e definições, conforme o perfil.
- Modos Claro, Escuro e Sistema, com preferência persistente e sem clarão ao
  carregar; contraste adequado e gráficos legíveis nos dois temas.
- O conteúdo original dos documentos mantém-se legível; não inverter as cores
  dos PDFs.
- Português europeu. Formulários com etiquetas, foco visível e mensagens claras.
  Estados vazios com indicação do próximo passo. Confirmação antes de anular
  movimentos.
- Página inicial após o login: o dashboard do perfil.

## Gmail (consulta e análise manual por defeito)

Conta exclusiva do condomínio, API oficial com OAuth apenas de leitura. Sem
envio, eliminação ou alteração de emails.

- Lista paginada, pesquisa e filtro por datas; assunto, remetente, destinatários,
  data e corpo completo; mensagens da conversa por ordem; anexos com nome, tipo
  e tamanho; PDFs abertos no navegador, incluindo documentos bancários;
  descarga de anexos.
- Atualização por botão; não descarregar a conta inteira nem todos os anexos
  antecipadamente; carregar por necessidade.
- HTML sanitizado e isolado, sem scripts nem conteúdo remoto automático; texto
  simples quando existir; visualizador de PDF do navegador.
- A partir do email ou documento: registar pagamento de condómino, registar
  despesa, associar a registo existente, marcar como tratado. O administrador
  preenche e confirma os campos; o documento mantém-se acessível durante o
  trabalho; cópia dos documentos associados e referência à origem; aviso sobre
  documentos já associados.
- Um extrato pode ser guardado e consultado sem criar despesa. Um comprovativo
  não confirma automaticamente a entrada de dinheiro.

## Definições e integrações

"Definições → Integrações", acessível apenas ao administrador.

Gmail: configuração de credenciais OAuth pelo administrador; endereço de
redirecionamento visível com instruções curtas; ligar conta, testar ligação,
desligar; conta ligada e estado.

IA: ativar/desativar (inicialmente desativada); endereço base de API compatível
com OpenAI; chave API quando necessária; identificador do modelo; tempo limite;
teste explícito da ligação.

- Nada fixado no código: endereço, modelo e credenciais configuráveis.
- Configurações persistentes; segredos guardados no servidor, fora do
  repositório e dos logs; segredos nunca devolvidos ao navegador.

## IA opcional

- "Analisar com IA" no email ou documento selecionado; a API só é chamada por
  clique explícito.
- Sugestões editáveis; nenhum movimento financeiro criado sem confirmação.
- O conteúdo enviado é tratado como dados, nunca como instruções a executar.
- PDFs com texto podem ser analisados; digitalizados sem suporte a imagens do
  modelo: informar a limitação e manter a análise manual. OCR local opcional
  (Tesseract), quando instalado.
- Um documento de cada vez, sem bloquear a navegação, com limites de tamanho e
  tempo. Serviço de IA indisponível: a aplicação continua funcional.

## Manutenção

Campos: título, descrição, localização, prioridade, estado, responsável opcional,
datas, observações, anexos, orçamentos e despesas associados. Distinguir causa
suspeita de causa confirmada. Assuntos da ata importados apenas por ação
explícita, com fonte e data.

## Assembleias e documentos

Guardar data, ordem de trabalhos, presença, votos por proposta e permilagens,
decisões, tarefas, ata e anexos. Somar votos sem declarar validade jurídica.
Distinguir unanimidade dos presentes de unanimidade de todas as frações.
Documentos: atas, faturas, comprovativos, contratos e orçamentos. OCR local
opcional (Tesseract). Documentos partilhados com condóminos apenas quando
explicitamente
partilhados.

## Exportação e backups

- CSV de quotas, pagamentos e despesas, com proteção contra fórmulas.
- Resumo mensal imprimível.
- Backup consistente da base SQLite e documentos, com restauro testado num
  destino isolado. Anexos e credenciais protegidos contra acesso público.
- Não apresentar uma simples cópia de uma base ativa como universalmente segura.
- Instalação: não publicar na Internet; quando chegar o momento, perguntar o
  que faltar (sistema operativo do servidor incluído) para instruções adequadas.

## Demonstração

Comando opcional com dados fictícios numa base separada. Recusar sobrescrever
base com dados. Nunca inserir pagamentos fictícios na configuração real.

## Entregas

1. Autenticação, perfis e estrutura visual comum com modo escuro.
2. Regras configuráveis e quotas extraordinárias.
3. Despesas e dashboard financeiro completo.
4. Gmail e IA opcional configurável.
5. Preparação da instalação e cópias de segurança.

Cada entrega deixa o projeto executável, com testes proporcionais, e reutiliza
o que já está concluído. Na primeira entrega o dashboard mostra apenas
informação real já disponível.

## Testes de aceitação essenciais

- Soma exata das permilagens; geração idempotente das cobranças mensais.
- Valores corretos por período após confirmação das regras.
- Dívidas antigas preservadas após mudanças de regras.
- Pagamentos parciais, adiantamentos e afetações sem duplicar receitas.
- Anulações revertem efeitos; transações inválidas sem alterações parciais.
- Edição de pagamentos: campos ausentes não limpam distribuição; retirada
  total exige confirmação explícita.
- Autenticação: rotas protegidas no servidor; condómino não acede a dados de
  outras frações; primeiro administrador criado sem palavra-passe predefinida.
- Despesas pendentes excluídas das saídas de caixa; fundo separado sem dupla
  contagem no saldo total.
- Dados existentes preservados nas inicializações; nada fictício na base real.
