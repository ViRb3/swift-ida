import ida_idaapi
import ida_kernwin
import ida_ida
import ida_hexrays
import idc
import re

# TODO: This is very rudimentary and prone to breakage, e.g. on lambda functions
# Ideally, we would want to implement a proper C grammar
func_regex = re.compile(r"^(.+?)(?:@<(.*?)>)?\((.+)?\)$")

action_names = []
arch_ret_regs = []
arch_arg_regs = []
arch_special_regs = []


class SwiftIDA(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_HIDE
    comment = "SwiftIDA Plugin"
    help = "SwiftIDA Plugin"
    wanted_name = "SwiftIDA"
    wanted_hotkey = ""
    dialog = None

    def init(self):
        global action_names, arch_ret_regs, arch_arg_regs, arch_special_regs

        arch_name: str = ida_ida.inf_get_procname()
        if arch_name.lower() == "metapc" and ida_ida.inf_is_64bit():
            # https://github.com/swiftlang/swift/blob/main/docs/ABI/CallConvSummary.rst#x86-64
            arch_ret_regs = ["rax", "rdx", "rcx", "r8"]
            arch_arg_regs = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
            arch_special_regs = ["r13", "r12", "r14"]
        elif arch_name.lower() == "arm" and ida_ida.inf_is_64bit():
            # https://github.com/swiftlang/swift/blob/main/docs/ABI/CallConvSummary.rst#arm64
            arch_ret_regs = [f"x{j}" for j in range(8)]
            arch_arg_regs = [f"x{j}" for j in range(8)]
            arch_special_regs = [f"x{j}" for j in range(20, 23)]

        if len(arch_ret_regs) == 0:
            # unsupported arch
            return ida_idaapi.PLUGIN_SKIP

        action_name = f"SwiftIDA:convert_to_usercall"
        action = ida_kernwin.action_desc_t(
            action_name,
            "Convert to usercall",
            generic_handler(lambda ea: self.convert_to_usercall(ea)),
        )
        ida_kernwin.register_action(action)
        action_names.append(action_name)

        arg_names = {0: "self", 1: "error_return", 2: "async_context"}
        for i in range(0, 3):
            action_name = f"SwiftIDA:add_arg_{arg_names[i]}"
            action = ida_kernwin.action_desc_t(
                action_name,
                f"Add argument {arg_names[i]}",
                generic_handler(lambda ea, i=i: self.add_callee_arg(ea, i)),
            )
            ida_kernwin.register_action(action)
            action_names.append(action_name)

        for i in range(2, len(arch_ret_regs) + 1):
            action_name = f"SwiftIDA:make_multi_{i}"
            action = ida_kernwin.action_desc_t(
                action_name,
                f"Make multi-return tuple{i}",
                generic_handler(lambda ea, i=i: self.make_multi_return(ea, i)),
            )
            ida_kernwin.register_action(action)
            action_names.append(action_name)

        self.ui_hooks = SwiftIDAUIHooks()
        self.ui_hooks.hook()

        return ida_idaapi.PLUGIN_KEEP

    def parse_current_func_type(self, ea):
        global func_regex, arch_arg_regs
        type: str = idc.get_type(ea)
        if type is None:
            ida_kernwin.warning("The selected item is not a function definition!")
            return None, None, None
        print(f">>>SwiftIDA: Original type: {type}")

        match = func_regex.match(type)

        base = match.group(1).split(" ")
        for i in range(1, len(base)):
            # [char, *_fastcall] -> [char*, _fastcall]
            while base[i].startswith("*"):
                base[i - 1] += "*"
                base[i] = base[i][1:]

        ret_regs = [] if match.group(2) is None else match.group(2).split(", ")
        if len(ret_regs) == 0 and base[0].lower() != "void":
            ret_regs.append(f"0:{arch_ret_regs[0]}")

        args = [] if match.group(3) is None else match.group(3).split(", ")
        if "__fastcall" in base:
            args = [
                f"{arg.split('@')[0]}@<{arch_arg_regs[i]}>"
                for i, arg in enumerate(args)
            ]
        if "__swiftcall" in base:
            raise Exception("__swiftcall not supported, remove it first")

        for item in ["__fastcall", "__usercall"]:
            if item in base:
                base.remove(item)

        return base, ret_regs, args

    def update_current_func_type(self, ea, base, ret_regs, args):
        ret_part = f"@<{', '.join(ret_regs)}>" if len(ret_regs) > 0 else ""
        new_type = f"{' '.join(base)} __usercall func{ret_part}({', '.join(args)})"
        print(f">>>SwiftIDA: New type: {new_type}")

        result = idc.SetType(ea, new_type)
        if result != 1:
            raise Exception("Failed to set type")

        print(f">>>SwiftIDA: Type changed successfully")

    def add_callee_arg(self, ea, i: int) -> bool:
        global arch_special_regs

        base, ret_regs, args = self.parse_current_func_type(ea)
        if base is None or ret_regs is None or args is None:
            return False

        if not any(f"@<{arch_special_regs[i]}>" in arg for arg in args):
            args.append(f"__int64@<{arch_special_regs[i]}>")

        self.update_current_func_type(ea, base, ret_regs, args)
        return True

    def convert_to_usercall(self, ea) -> bool:
        global arch_ret_regs

        base, ret_regs, args = self.parse_current_func_type(ea)
        if base is None or ret_regs is None or args is None:
            return False

        self.update_current_func_type(ea, base, ret_regs, args)
        return True

    def make_multi_return(self, ea, i: int) -> bool:
        global arch_ret_regs

        struct_name = f"swiftida_tuple{i}"
        if idc.get_struc_id(struct_name) == idc.BADADDR:
            struct_id = idc.add_struc(-1, struct_name, 0)
            for j in range(i):
                idc.add_struc_member(struct_id, f"o{j}", -1, idc.FF_QWORD, -1, 8)
            print(f">>>SwiftIDA: Created struct {struct_name}")

        base, ret_regs, args = self.parse_current_func_type(ea)
        if base is None or ret_regs is None or args is None:
            return False

        base[0] = struct_name

        ret_regs = [f"{j*8}:{arch_ret_regs[j]}" for j in range(i)]

        self.update_current_func_type(ea, base, ret_regs, args)
        return True


class SwiftIDAUIHooks(ida_kernwin.UI_Hooks):
    def finish_populating_widget_popup(self, form, popup):
        global action_names
        if ida_kernwin.get_widget_type(form) not in [
            ida_kernwin.BWN_DISASM,
            ida_kernwin.BWN_PSEUDOCODE,
        ]:
            return
        for name in action_names:
            ida_kernwin.attach_action_to_popup(form, popup, name, "SwiftIDA/")


def generic_handler(callback):
    class Handler(ida_kernwin.action_handler_t):
        def __init__(self):
            ida_kernwin.action_handler_t.__init__(self)

        def activate(self, ctx):
            try:
                if ctx.widget_type == ida_kernwin.BWN_PSEUDOCODE:
                    vu = ida_hexrays.get_widget_vdui(ctx.widget)
                    if vu.item.citype == ida_hexrays.VDI_FUNC:
                        ea = vu.item.f.entry_ea
                    else:
                        ea = vu.item.e.obj_ea
                else:
                    #TODO: Support call operand in disassembly view
                    ea = ida_kernwin.get_screen_ea()

                result = callback(ea)

                if result:
                    if ctx.widget_type == ida_kernwin.BWN_PSEUDOCODE:
                        vu = ida_hexrays.get_widget_vdui(ctx.widget)
                        vu.refresh_view(True)
            except Exception as e:
                ida_kernwin.warning("There was an error, check logs")
                raise e
            return 1

        def update(self, ctx):
            return ida_kernwin.AST_ENABLE_ALWAYS

    return Handler()


def PLUGIN_ENTRY():
    return SwiftIDA()
