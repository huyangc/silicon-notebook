"""Pure knowledge-graph domain contracts (sunk from app.services.kg in B3).

Leaf package: no app.services/app.repositories dependency. Individual
modules here mirror pure, frequently-imported pieces of app.services.kg so
app.repositories adapters can depend on them without a reverse dependency on
app.services.
"""
