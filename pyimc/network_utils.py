import netifaces


def get_interfaces():
    """
    Retrieves the address of all external interfaces (lo 127.0.0.1 ignored)
    :return: List of tuples (interface, addr)
    """
    interfaces = netifaces.interfaces()
    if_ext = []
    for i in interfaces:
        if i == 'lo':
            continue
        iface = netifaces.ifaddresses(i).get(netifaces.AF_INET)
        if iface:
            for j in iface:
                if_ext.append((i, j['addr']))

    return if_ext


if __name__ == '__main__':
    i = get_interfaces()
    print(i)