# codex-flow

Servidor MCP por stdio para ejecutar roles de CLI en un popup de tmux o en segundo plano. Python 3.9+ y biblioteca estándar, sin dependencias Python adicionales.

## Instalación

Requiere Python 3.9+, Git y los programas que uses (`codex`, `claude`, `agy`) en PATH. Para los roles con popup también requiere tmux. Configura previamente la autenticación de cada CLI y la identidad de Git para poder crear commits.

```sh
claude mcp add -s user codex-flow -- python3 /ruta/a/codex-flow/server.py
agy mcp add codex-flow python3 /ruta/a/codex-flow/server.py
```

Si la ruta contiene espacios, enciérrala entre comillas. Para usar popups, inicia Claude o agy dentro de tmux para que el servidor herede `TMUX`. Los roles en segundo plano no requieren tmux. Ejecuta `doctor` para comprobar el entorno.

## Herramientas

- `run_role`: su descripción lista los roles configurados en cada consulta. Recibe `role` y `task` (strings requeridos), y `cwd` (opcional; por defecto el directorio del servidor). Ejemplo: `{"role":"implementer","task":"Corrige el error descrito","cwd":"/ruta/al/repo"}`.
- `doctor`: sin argumentos; informa `ok` / `falta` para tmux, la sesión, los programas usados por los roles, el script de cierre y el JSON del usuario. Siempre devuelve `isError: false`.

`run_role` comprueba que `cwd` sea un repositorio Git. Con `snapshot: true` (valor predeterminado), ejecuta `git add -u` y crea un commit `wip` si hay cambios preparados. Incluye cambios que ya estaban staged; los archivos nuevos sin seguimiento no se añaden automáticamente. Si no hay cambios continúa; en un repositorio sin commits crea un commit vacío para disponer de HEAD. Los errores de comprobación o de la foto Git impiden ejecutar el rol. Con `snapshot: false` no prepara ni confirma cambios.

El popup ocupa el 95% del ancho y el 90% del alto y bloquea la llamada hasta cerrarse. La respuesta incluye el código de salida y `git diff --stat HEAD`, sin el diff completo. Esa estadística no incluye archivos sin seguimiento ni cambios ya confirmados por el CLI. Codex utiliza el script `close-popup.sh` para cerrar el popup al terminar el turno principal; con otros CLI sal del programa para cerrar el popup.

En segundo plano se ejecuta el CLI sin entrada interactiva y se devuelve stdout. Si supera 20000 caracteres, se conservan los últimos 20000 con un aviso. Un código distinto de cero devuelve `isError: true`, el código y los últimos 4000 caracteres de stderr; un programa inexistente o un timeout también devuelve error. Al final se añade `git diff --stat HEAD` si tiene contenido. Si no se puede obtener la estadística (por ejemplo, sin HEAD y con `snapshot: false`), se informa sin ocultar el resultado del CLI. El timeout solo se aplica al segundo plano.

## Configuración

Se carga `config.json` junto al servidor y después `~/.config/codex-flow/config.json`, si existe. Cada proveedor o rol del usuario reemplaza **la entrada completa** con el mismo nombre; los demás se conservan. No hay fusión profunda de sus campos.

Roles incluidos: `planner` (Claude, segundo plano, sin foto), `implementer` (Codex, popup, con foto), `reviewer` (Claude, segundo plano, sin foto) y `fixer` (Claude, segundo plano, con foto). Puedes cambiar cualquiera o añadir nombres propios.

| Clave de rol | Tipo | Por defecto | Uso |
|---|---|---|---|
| `provider` | string | requerido | Nombre en `providers` |
| `model` | string | requerido si se usa `{model}` | Modelo del CLI |
| `effort` | string | requerido si se usa `{effort}` | Esfuerzo del modelo |
| `popup` | bool | `false` | Selecciona `popup` o `background` |
| `snapshot` | bool | `true` | Guarda la foto Git `wip` antes de ejecutar |
| `args` | lista de strings | `[]` | Argumentos propios, incluidos permisos |
| `timeout` | número positivo finito | `1800` | Segundos; solo segundo plano |

Ejemplo de `~/.config/codex-flow/config.json` para añadir un rol propio:

```json
{
  "roles": {
    "traductor": {
      "provider": "claude",
      "model": "claude-sonnet-5-5",
      "effort": "medium",
      "snapshot": false,
      "args": ["--allowed-tools", "Read,Grep,Glob"],
      "timeout": 120
    }
  }
}
```

Otro ejemplo para cambiar el revisor a `agy`:

```json
{
  "roles": {
    "reviewer": {
      "provider": "agy",
      "model": "TU_MODELO_AGY",
      "effort": "medium",
      "popup": false,
      "snapshot": false,
      "args": []
    }
  }
}
```

Sustituye `TU_MODELO_AGY` por un modelo disponible en tu CLI. En `claude -p` y `agy -p` los permisos se dan mediante `args` (por ejemplo, `--allowed-tools` en Claude y `--mode` en agy); usa los valores admitidos por tu instalación.

Nota sobre `agy` en segundo plano: no uses `--mode plan`, porque guarda el plan en un archivo interno de agy y no lo devuelve por stdout. `agy -p` sin ventana bloquea por sí solo las herramientas que piden permiso (escritura, comandos), así que sirve como rol de solo lectura sin args extra. Si el rol termina con código 0 pero sin salida (p. ej. una herramienta bloqueada), el servidor devuelve `isError: true` con el stderr. Ejemplo de `planner` con agy:

```json
{"provider": "agy", "model": "gemini-3.8-flash-high", "effort": "high", "popup": false, "snapshot": false}
```

Las reglas de permisos de los roles incluidos son exactas (sin `:*`) porque `git diff --output=<archivo>` escribe archivos. Cada comando se incluye también con la variante `rtk` porque algunos hooks reescriben los comandos antes de comprobar permisos; si usas otro reescritor, añade su variante.

Los roles de Claude añaden `--tools`, que limita las herramientas disponibles (quita los subagentes, incluso si la configuración global los permite): `Read,Grep,Glob,Bash` en `planner` y `reviewer`, y `Read,Grep,Glob,Edit,Write,Bash` en `fixer`. En `fixer`, `Edit(./**)` y `Write(./**)` limitan la escritura al repositorio (rutas relativas al `cwd` del rol). No se usa `--permission-mode acceptEdits` porque permite `rm` y `touch` y escribir fuera del repositorio.

Los proveedores definen `type: "cli"` y hasta dos listas de argumentos: `popup` (interactiva) y `background` (salida por stdout). Debe existir la plantilla del modo elegido. El elemento exacto `"{args}"` se expande en los argumentos del rol, en su lugar; sin argumentos desaparece. Los marcadores `{prompt}`, `{model}`, `{effort}` y `{close_popup}` se sustituyen dentro de la plantilla y de los argumentos en una sola pasada: los marcadores que aparezcan en los valores insertados quedan literales. Un marcador requerido sin valor produce un error. `{close_popup}` apunta al script junto al servidor. Con opciones que aceptan varios valores (p. ej. `--allowed-tools` de `claude`), poner `"--"` antes de `"{prompt}"` en la plantilla; si no, la opción se traga el prompt.

En popup se escapan los argumentos al construir el comando de tmux; en segundo plano se pasa la lista directamente al proceso. `task` no puede empezar con `-`. No se incluyen proveedores API.

## Seguridad

El servidor funciona sin red y sin puertos: solo lee y escribe mensajes JSON por stdio. Los CLI invocados pueden usar red según su funcionamiento. Para claves de API, usa solo variables de entorno; no las guardes en la configuración ni en el repositorio. La configuración contiene comandos ejecutables y debe proceder de una fuente de confianza.

Codex se inicia con sandbox `read-only` y aprobaciones `on-request`: puede solicitar autorización para escribir. Cuando está activada, la foto Git modifica el índice y puede crear un commit antes de ejecutar el rol. Usa `cwd` para señalar el repositorio que quieres modificar.

Guardia anti-cadena: cada rol lanzado recibe `CODEX_FLOW_ROLE=<rol>` (en popup mediante `tmux display-popup -e`, requiere tmux 3.3 o superior) y `run_role` se niega a ejecutarse si esa variable existe, para que un rol no pueda lanzar otros roles.

## Pruebas

```sh
python3 -m unittest test_server.py
```

Las pruebas usan repositorios temporales y simulan los proveedores de ambos modos; no requieren una sesión tmux ni ejecutan proveedores reales.
