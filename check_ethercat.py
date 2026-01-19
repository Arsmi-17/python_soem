#!/usr/bin/env python3
import pysoem

def main():
    print("=" * 50)
    print("EtherCAT Network & Slave Check")
    print("=" * 50)

    # 1. Find network adapters
    adapters = pysoem.find_adapters()

    if not adapters:
        print("❌ No network adapters found")
        return

    print("\nAvailable Network Interfaces:")
    for i, a in enumerate(adapters):
        print(f"  {i}: {a.desc} ({a.name})")

    # 2. User selects interface
    try:
        iface_id = int(input("\nEnter interface ID: "))
    except ValueError:
        print("❌ Invalid input (not a number)")
        return

    if iface_id < 0 or iface_id >= len(adapters):
        print("❌ Interface ID out of range")
        return

    iface = adapters[iface_id].name
    print(f"\nUsing interface: {iface}")

    # 3. Open master
    master = pysoem.Master()
    master.open(iface)

    # 4. Scan for slaves
    slave_count = master.config_init()

    if slave_count <= 0:
        print("❌ No EtherCAT slaves detected")
        master.close()
        return

    print(f"\n✅ EtherCAT Slaves Found: {slave_count}")

    # 5. Print slave details
    for i, slave in enumerate(master.slaves):
        print(f"  Slave {i}:")
        print(f"    Name        : {slave.name}")
        print(f"    Product Code: {hex(slave.id)}")

    # 6. Cleanup
    master.close()
    print("\nCheck completed successfully.")

if __name__ == "__main__":
    main()
