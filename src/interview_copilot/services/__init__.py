"""v2 application services (spec 5.6).

Services own lifecycle wiring (threads, queues, cancellation) and call
into domain + providers; they never import FastAPI and never persist
mutable global state.
"""
