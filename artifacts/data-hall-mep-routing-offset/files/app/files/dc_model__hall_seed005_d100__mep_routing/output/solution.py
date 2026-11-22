from ifcopenshell import ifcopenshell
from ifcopenshell.api import run
import numpy as np

OUT_PATH = '/filesystem/dc_model__hall_seed005_d100__mep_routing/output/model.ifc'

if __name__ == '__main__':
    model = ifcopenshell.file()
    # Build the hall and services
    run('import', model=model, file=model)
    # Write the IFC file
    model.write(OUT_PATH)