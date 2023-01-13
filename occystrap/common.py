import json

from occystrap.constants import RUNC_SPEC_TEMPLATE


def write_container_config(container_config_filename, runtime_config_filename,
                           container_template=RUNC_SPEC_TEMPLATE,
                           container_values=None):
    if not container_values:
        container_values = {}

    # Read the container config
    with open(container_config_filename) as f:
        image_conf = json.loads(f.read())

    # Write a runc specification for the container
    container_conf = json.loads(container_template)

    container_conf['process']['terminal'] = True
    cwd = image_conf['config']['WorkingDir']
    if cwd == '':
        cwd = '/'
    container_conf['process']['cwd'] = cwd

    entrypoint = image_conf['config'].get('Entrypoint', [])
    if not entrypoint:
        entrypoint = []
    cmd = image_conf['config'].get('Cmd', [])
    if cmd:
        entrypoint.extend(cmd)
    container_conf['process']['args'] = entrypoint

    # terminal = false means "pass through existing file descriptors"
    container_conf['process']['terminal'] = False

    container_conf['hostname'] = container_values.get(
        'hostname', 'occystrap')

    with open(runtime_config_filename, 'w') as f:
        f.write(json.dumps(container_conf, indent=4, sort_keys=True))
