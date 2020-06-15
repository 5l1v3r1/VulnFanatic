from binaryninja import *
import re
from ..utils.utils import extract_hlil_operations


class FreeScanner2(BackgroundTaskThread):
    def __init__(self,bv):
        self.current_view = bv
        self.progress_banner = f"[VulnFanatic] Running the scanner ... "
        self.banner = f"[VulnFanatic] Running the scanner ... "
        BackgroundTaskThread.__init__(self, self.progress_banner, True)
        #self.free_list = ["free","_free","_freea","freea","free_dbg","_free_dbg","free_locale","_free_locale","operator delete"]
        self.free_list = ["free","_free","_freea","freea","free_dbg","_free_dbg","free_locale","_free_locale"]

    def run(self):
        free_xrefs = self.get_xrefs_with_wrappers()
        counter = 1
        # With all wrappers detected lets do the scan
        for free_xref in free_xrefs:
            self.progress_banner = self.banner + f"scanning free xref #{counter}"
            counter += 1
            param_vars = self.prepare_relevant_variables(free_xref["instruction"].params[free_xref["param_index"]])
            uaf,uaf_if,not_init,null_set = self.scan(free_xref["instruction"],param_vars)
            current_free_xref_obj = {
                "used_after": uaf,
                "without_if": uaf_if,
                "not_initialized_before_free": not_init,
                "is_set_to_null": null_set,
                "struct_free_wrapper": free_xref["struct_free_wrapper"]
            }
            # First process parameter variables
            
            log_info(str(current_free_xref_obj))

    def scan(self,instruction,param_vars):
        
        current_hlil_instructions = list(instruction.function.instructions)
        # Check if instruction is in loop so that we know how to proceed with checks further
        in_loop = self.is_in_loop(instruction)
        instructions = []
        if in_loop["in_loop"]:
            # In loop
            for i in in_loop["loop"].body.lines:
                instructions.append(i.il_instruction)
            # Keep natural order of instructions for loops
            '''ins_index = -1
            while ins_index != instruction.instr_index:
                ins = instructions.pop(0)
                instructions.append(ins)
                if ins:
                    ins_index = ins.instr_index
                else:
                    break'''
        else:
            # set instructions to go through at current instruction -1, this will ensure that we do no lose info if null is set before a call to delete operator for example
            instructions = current_hlil_instructions[instruction.instr_index-1:]
        # TODO here
        # Check if param set to null
        is_set_to_null = self.is_set_to_null(instructions,param_vars)
        
        # Check if param is used after the free call, if not in loop get rid of first instruction
        if not in_loop["in_loop"]:
            used_after, used_after_with_if = self.used_after(instructions[1:],param_vars,instruction)
            not_initialized_before_free = False
        else:
            not_initialized_before_free = self.get_preallocations(instruction,param_vars,instructions,in_loop["loop"].il_basic_block.start)
            used_after, used_after_with_if = self.used_after(instructions,param_vars,instruction)
        return used_after, used_after_with_if, not_initialized_before_free, is_set_to_null

    def is_set_to_null(self,instructions,param_vars):
        for i in instructions:
            if i:
                for param in param_vars["possible_values"]:
                    if i.operation == HighLevelILOperation.HLIL_ASSIGN and re.search(param,str(i.dest)):
                        # one of the possible values was found in the instruction which assigns value
                        if (i.src.operation == HighLevelILOperation.HLIL_CONST or i.src.operation == HighLevelILOperation.HLIL_CONST_PTR) and i.src.constant == 0:
                            # Null assgined -> return True
                            return True 
        return False

    def used_after(self,instructions,param_vars,instruction):
        uaf = False
        uaf_if = False
        for i in instructions:
            if i and i.instr_index != instruction.instr_index:
                for param in param_vars["possible_values"]:
                    if (re.search(param,str(i)) and not (i.operation == HighLevelILOperation.HLIL_ASSIGN and re.search(param,str(i.dest))) and not i.operation == HighLevelILOperation.HLIL_IF):
                        if self.not_if_dependent(instruction,param_vars):
                            uaf_if = True
                        uaf = True
        return uaf, uaf_if

    def get_xrefs_with_wrappers(self):
        free_xrefs = []
        for xref in self.get_xrefs_to_call(self.free_list):
            append = True
            param_vars = self.prepare_relevant_variables(xref.params[0])
            for var in param_vars["vars"]:
                if var in xref.function.source_function.parameter_vars:
                    wrapper_xrefs = self.get_xrefs_to_call([xref.function.source_function.name])
                    if wrapper_xrefs:
                        for wrapper_xref in wrapper_xrefs:
                            free_xrefs.append({
                                "instruction": wrapper_xref,
                                "param_index": list(xref.function.source_function.parameter_vars).index(var),
                                "struct_free_wrapper": False
                            })
                    else:
                        # No xrefs -> struct free wrapper???
                        free_xrefs.append({
                            "instruction": xref,
                            "param_index": 0,
                            "struct_free_wrapper": True
                        })
                elif append:
                    free_xrefs.append({
                        "instruction": xref,
                        "param_index": 0, # All the default free calls take just one parameter
                        "struct_free_wrapper": False
                    })
                    append = False
        return free_xrefs

    def prepare_relevant_variables(self,param):
        param_vars = extract_hlil_operations(param.function,[HighLevelILOperation.HLIL_VAR],specific_instruction=param)
        vars = {
            "possible_values": [],
            "vars": []
        }
        tmp_possible = [str(param)]
        for var in param_vars:
            if var.var not in vars["vars"]:
                vars["vars"].append(var.var)
            definitions = param.function.get_var_definitions(var.var)
            # Also uses are relevant
            definitions.extend(param.function.get_var_uses(var.var))
            for d in definitions:
                if (d.operation == HighLevelILOperation.HLIL_VAR_INIT or d.operation == HighLevelILOperation.HLIL_ASSIGN)and type(d.src.postfix_operands[0]) == Variable and d.src.postfix_operands[0] not in vars["vars"]:
                    val = str(param).replace(str(var.var),str(d.src.postfix_operands[0]))
                    tmp_possible.append(val)
                    vars["vars"].append(d.src.postfix_operands[0])
                    for v in vars["vars"]:
                        val.replace(str(v),str(v)+"\\:?\\d*\\.?\\w*")
        for val in tmp_possible:
            tmp_val = val
            positions = [(m.start(0), m.end(0)) for m in re.finditer(r':\d+\.\w+', val)]
            for pos in positions:
                tmp_val = val[0: pos[0]:] + val[pos[1]::]
            tmp_val = re.escape(tmp_val)
            for v in vars["vars"]:
                # Lol but worth a try :D
                tmp_val = tmp_val.replace(str(v),str(v)+"(:\\d+\\.\\w+)?\\b")
            vars["possible_values"].append(tmp_val)
        log_info(str(vars["possible_values"]))               
        return vars

    # TODO this is probably ok
    def is_in_loop(self,instruction):
        loop_object = {"loop":None,"in_loop":False}
        parent = instruction.parent
        while parent != None:
            if parent.operation == HighLevelILOperation.HLIL_DO_WHILE or parent.operation == HighLevelILOperation.HLIL_FOR or parent.operation == HighLevelILOperation.HLIL_WHILE:
                loop_object = {"loop":parent,"in_loop":True}
                return loop_object
            parent = parent.parent
        return loop_object

    # TODO this needs to be reworked to detect if the variable that is related to the free call is being used in the assignment
    def not_if_dependent(self,instruction,param_vars):
        if_dep = True
        parent = instruction.parent
        while parent != None:
            if parent.operation == HighLevelILOperation.HLIL_IF:
                for param in param_vars["possible_values"]:
                    if re.search(param,str(parent)):
                        if_dep = False 
            parent = parent.parent
        return if_dep

    # TODO does not work
    def get_preallocations(self,instruction,param_vars,hlil_instructions,loop_boundary):
        root = {
            "block":instruction.il_basic_block,
            "start":instruction.il_basic_block.start,
            "end":instruction.instr_index,
            "alloc":False,
            "blocks_on_current_path":[instruction.il_basic_block.start],
            "dominators":[]
            }
        blocks = [root]
        while blocks:
            for param_var in param_vars["possible_values"]:
                current_block = blocks.pop()
                for inst_index in range(current_block["start"],current_block["end"]):
                    if hlil_instructions[inst_index]:
                        inst_string = str(hlil_instructions[inst_index])
                        if ("alloc" in inst_string and re.search(param_var,inst_string)) or (hlil_instructions[inst_index].operation == HighLevelILOperation.HLIL_ASSIGN and re.search(param_var,str(hlil_instructions[inst_index].dest))):
                            current_block["alloc"] = True
                # Check incoming branches and populate blocks list if there are any
                if current_block["block"].incoming_edges and current_block["start"] != loop_boundary:
                    for b in current_block["block"].incoming_edges:
                        if b.source.start not in current_block["blocks_on_current_path"]:
                            current_block["blocks_on_current_path"].append(b.source.start)
                            source = {
                                "block":b.source,
                                "start":b.source.start,
                                "end":b.source.end,
                                "alloc":current_block["alloc"],
                                "blocks_on_current_path":current_block["blocks_on_current_path"].copy(),
                                "dominators":[]
                                }
                            current_block["dominators"].append(source)
                            blocks.append(source)
                elif not current_block["alloc"]:
                    # No incoming edges -> top of the trace -> if path without alloc was found we can happily return False as path without alloc exists
                    return False
        #log_info(str(root))
        return True

    def get_xrefs_to_call(self,function_names):
        checked_functions = []
        xrefs = []
        for symbol_name in function_names:
            symbol_item = []
            try:
                symbol_item.extend(self.current_view.symbols[symbol_name]) if type(self.current_view.symbols[symbol_name]) is list else symbol_item.append(self.current_view.symbols[symbol_name])
            except KeyError:
                pass
            try:
                symbol_item.extend(self.current_view.symbols[symbol_name+"@IAT"]) if type(self.current_view.symbols[symbol_name+"@IAT"]) is list else symbol_item.append(self.current_view.symbols[symbol_name+"@IAT"])
            except KeyError:
                pass
            try:
                symbol_item.extend(self.current_view.symbols[symbol_name+"@PLT"]) if type(self.current_view.symbols[symbol_name+"@PLT"]) is list else symbol_item.append(self.current_view.symbols[symbol_name+"@PLT"])
            except KeyError:
                pass
            # Operator Delete refs -> TODO this takes long time -> REWORK for sure
            if symbol_name == "operator delete":
                for sym in self.current_view.symbols:
                    if type(self.current_view.symbols[sym]) is list:
                        for item in self.current_view.symbols[sym]:
                            if "operator delete" in item.full_name and item not in symbol_item:
                                symbol_item.append(item)
                    elif "operator delete" in self.current_view.symbols[sym].full_name and self.current_view.symbols[sym] not in symbol_item:
                        symbol_item.append(self.current_view.symbols[sym])
            for symbol in symbol_item if type(symbol_item) is list else [symbol_item]:
                for ref in self.current_view.get_code_refs(symbol.address):
                    # Get exact instruction index
                    if ref.function.name in checked_functions:
                        continue
                    else:
                        checked_functions.append(ref.function.name)
                    for instruction in ref.function.hlil.instructions:
                        # For each instruction check if any of the functions we are looking for is called
                        for f in function_names:
                            if f in str(instruction):
                                # Extract the call here
                                calls = extract_hlil_operations(instruction.function,[HighLevelILOperation.HLIL_CALL],specific_instruction=instruction)
                                for call in calls:
                                    if str(call.dest) in function_names and call not in xrefs:
                                        xrefs.append(call)
        #log_info(str(xrefs))
        return xrefs