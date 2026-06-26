"""UHI-Battery: Urban Heat Island → e-micromobilite batarya optimizasyonu.

Modüller (P1+ ile doldurulacak):
- data:    GEE LST pull, MODIS-anomaly fusion, NASA PCoE loader, sim trips
- stats:   Moran's I + Getis-Ord Gi* (PySAL)
- models:  energy regression + SoH Arrhenius
- routing: OSMnx graph + pymoo NSGA-II Pareto frontier
- stations: charging-station optimization
- api:     FastAPI
- viz:     Streamlit dashboard
"""
__version__ = "0.1.0"
