"""O que o admin faz além de ler, uma função por ação, sem `Request`, HTML nem aviso de tela: recebe o
que precisa, devolve um resultado ou levanta uma exceção de domínio. A página (`app.admin.routes`) e a
API de admin (`app.admin.api`) a chamam e a traduzem, cada uma no seu formato (ADR 0015, "A página e
a API dividem os serviços"). Cada recurso tem o seu módulo.
"""
