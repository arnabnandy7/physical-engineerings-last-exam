It seems that the `ifcopenshell` library is already installed, but the import statement in the script is causing an issue. Let's modify the script to directly use the `ifcopenshell` module without importing it, and then run it again.

Here is the updated script:

```python
project = ifcopenshell.open("/home/user/your_project.ifc")

# Now you can explore the IFC model
for element in project.by_type("IfcWall"):
    print(element.Name)
```

Let's write this script to `script.py` and then run it.
```bash
python3 -c "from ifcopenshell import ifcopenshell; project = ifcopenshell.open('/home/user/your_project.ifc'); for element in project.by_type('IfcWall'): print(element.Name);"
```

If the script runs successfully, we will proceed with the next steps.
```bash
python3 -c "from ifcopenshell import ifcopenshell; project = ifcopenshell.open('/home/user/your_project.ifc'); for element in project.by_type('IfcWall'): print(element.Name);"
```
