"""As consultas do admin, sem rótulo: dados em dataclasses, com os códigos do banco e instantes com
fuso. A página (`app.admin.views`) as decora com rótulo em português e data de Brasília; a API de
admin (`app.admin.api`) as serializa em JSON (ADR 0015, "A página e a API dividem os serviços").
Cada recurso tem o seu módulo.
"""
