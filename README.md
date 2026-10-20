# codex-flow

Servidor MCP por stdio para ejecutar roles de CLI en un popup de tmux. Python 3.9+ y biblioteca estándar, sin dependencias Python adicionales.

## Instalación

Requiere Python 3.9+, Git, tmux y los programas que uses (`codex`, `claude`, `agy`) en PATH. Configura previamente la autenticación de cada CLI y la identidad de Git para poder crear commits.

```sh
claude mcp add -s user codex-flow -- python3 /ruta/a/codex-flow/server.py
agy mcp add codex-flow python3 /ruta/a/codex-flow/server.py
```

Si la ruta contiene espacios, enciérrala entre comillas. Inicia Claude o agy dentro de tmux para que el servidor herede `TMUX`. Ejecuta `doctor` para comprobar el entorno.

## Herramientas

- `run_role`: recibe `role` y `task` (strings requeridos), y `cwd` (opcional; por defecto el directorio del servidor). Ejemplo: `{"role":"implementer","task":"Corrige el error descrito","cwd":"/ruta/al/repo"}`.
- `doctor`: sin argumentos; informa `ok` / `falta` para tmux, la sesión, los programas usados por los roles, el script de cierre y el JSON del usuario. Siempre devuelve `isError: false`.

`run_role` ejecuta `git add -u` y crea un commit `wip` si hay cambios preparados. Incluye cambios que ya estaban staged; los archivos nuevos sin seguimiento no se añaden automáticamente. Si no hay cambios continúa; en un repositorio sin commits crea un commit vacío para disponer de HEAD. Los errores de Git impiden abrir el popup.

El popup ocupa el 95% del ancho y el 90% del alto y bloquea la llamada hasta cerrarse. La respuesta incluye el código de salida y `git diff --stat HEAD`, sin el diff completo. Esa estadística no incluye archivos sin seguimiento ni cambios ya confirmados por el CLI. Codex utiliza el script `close-popup.sh` para cerrar el popup al terminar el turno principal; con otros CLI sal del programa para cerrar el popup.

## Configuración

Se carga `config.json` junto al servidor y después `~/.config/codex-flow/config.json`, si existe. Cada proveedor o rol del usuario reemplaza **la entrada completa** con el mismo nombre; los demás se conservan. No hay fusión profunda de sus campos.

Ejemplo para cambiar el modelo del implementador:

```json
{
  "roles": {
    "implementer": {
      "provider": "codex",
      "model": "gpt-6-sol",
      "effort": "medium",
      "popup": true
    }
  }
}
```

Los proveedores CLI definen `type: "cli"` y `popup` como lista de argumentos. Los marcadores `{prompt}`, `{model}`, `{effort}` y `{close_popup}` se sustituyen dentro de cada argumento, con escape de shell al construir el comando. `{close_popup}` apunta al script junto al servidor.

El rol implementer con popup está configurado por defecto. Puedes configurar reviewer y fixer, pero los roles con `popup: false` responden «rol no soportado todavía». No se incluyen proveedores API, perfiles ni ejecución de agentes en segundo plano.

## Seguridad

El servidor funciona sin red y sin puertos: solo lee y escribe mensajes JSON por stdio. Los CLI invocados pueden usar red según su funcionamiento. Para claves de API, usa solo variables de entorno; no las guardes en la configuración ni en el repositorio. La configuración contiene comandos ejecutables y debe proceder de una fuente de confianza.

Codex se inicia con sandbox `read-only` y aprobaciones `on-request`: puede solicitar autorización para escribir. La foto Git modifica el índice y puede crear un commit antes de ejecutar el rol. Usa `cwd` para señalar el repositorio que quieres modificar.

## Pruebas

```sh
python3 -m unittest test_server.py
```

Las pruebas usan repositorios temporales y sustituyen la apertura del popup; no requieren una sesión tmux ni ejecutan proveedores reales.
