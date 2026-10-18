# Cómo subir este repositorio a tu GitHub

1. En GitHub: **New repository**, nombre `prusa-xyzcal-klipper`, sin README ni licencia (ya vienen en la carpeta).
2. En tu ordenador, dentro de esta carpeta:

```bash
git init -b main
git add .
git commit -m "Primera versión: calibración XYZ estilo Prusa para Klipper"
git remote add origin https://github.com/wencescarlos/prusa-xyzcal-klipper.git
git push -u origin main
```

3. Opcional: crea una *release* `v0.1.0` en GitHub y copia en ella el contenido de `CHANGELOG.md`.

Para publicar cambios después: `git add . && git commit -m "mensaje" && git push`. La Raspberry Pi los recibirá desde la pestaña de actualizaciones de Mainsail/Fluidd si configuraste `update_manager`.

## Nota sobre el permiso de ejecución de `install.sh`

Si subes desde Windows, Git puede perder el permiso de ejecución. Para dejarlo registrado:

```bash
git update-index --chmod=+x install.sh
git commit -m "install.sh ejecutable"
git push
```

Aun así, el README usa `bash install.sh`, que funciona sin ese permiso.
