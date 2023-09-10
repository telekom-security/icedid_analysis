import argparse
import base64
import binascii
import json
import logging
import magic
import os
import re
import subprocess
import sys
import tempfile
from typing import List

REGEX_B64_ALPHABET_CANDIDATES = rb"[A-Za-z0-9+/=]{64}"
REGEX_B64_STRINGS = rb"[A-Za-z0-9+/=]{5,}"
PE_START_BYTES = bytes.fromhex("4D5A50000200000004000F00FFFF00")
AU3_MAGIC_BYTES = b"AU3!EA06"


# =====================================================================
# Custom base64 decoding as implemented by rivitna:
# https://github.com/rivitna/Malware2/blob/main/DarkGate/dg_dec_data.py
def base64_decode_block(block, encode_table):
    if len(block) < 2:
        raise ValueError("Base64 decode error.")
    n = 0
    for i in range(4):
        n <<= 6
        if i < len(block):
            b = encode_table.find(block[i])
            if b < 0:
                raise ValueError("Base64 invalid char (%02X)." % block[i])
            n |= b

    dec_block = bytes([(n >> 16) & 0xFF, (n >> 8) & 0xFF])
    if len(block) >= 4:
        dec_block += bytes([n & 0xFF])

    return dec_block


def base64_decode(data, encode_table):
    dec_data = b""
    for block in (data[i : i + 4] for i in range(0, len(data), 4)):
        dec_data += base64_decode_block(block, encode_table)

    return dec_data


# =====================================================================


def get_alphabet_candidates(content: bytes) -> List[bytes]:
    """Identify and return characteristic strings in the binary that could be a custom base64 alphabet

    Args:
        content (bytes): PE file content

    Returns:
        List[bytes]: base64 alphabet candidates
    """
    result = []
    candidates = re.findall(REGEX_B64_ALPHABET_CANDIDATES, content)
    for candidate in candidates:
        candidate_string = candidate.decode()
        if (
            "".join(sorted(candidate_string))
            == "+0123456789=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        ):
            result.append(candidate)
    return result


def decode_strings(content: bytes, alphabet_candidates: List[bytes]) -> List[str]:
    """Try to decode encoded strings with the given custom base64 alphabet candidates

    Args:
        content (bytes): PE file content
        alphabet_candidates (List[bytes]): Custom base64 alphabet candidates

    Returns:
        List[str]: List of decoded strings
    """
    result = []
    string_candidates = re.findall(REGEX_B64_STRINGS, content)
    for s in string_candidates:
        for alphabet in alphabet_candidates:
            try:
                # Try to decode each string candidate with each alphabet candidate
                decoded = base64_decode(s, alphabet).decode()
                decoded_length = len(decoded)
                ascii_length = len(decoded.encode("ascii", "ignore"))
                # Rather simple check to sort out garbage strings
                if decoded_length == ascii_length:
                    result.append(decoded)
            except UnicodeDecodeError:
                pass
            except ValueError:
                pass
    return result


def perform_string_extraction(content: bytes) -> List[str]:
    """Tries to extract the encrypted/obfuscated strings from a DarkGate sample

    Args:
        content (bytes): PE file content

    Returns:
        List[str]: List of extracted strings. May be `None`, if the sample is not supported.
    """
    candidates = get_alphabet_candidates(content)
    if candidates:
        logging.info(
            f"Found candidates for custom base64 alphabet: {b', '.join(candidates).decode()}"
        )
        return decode_strings(content, candidates)
    else:
        logging.info(
            f"No candidates for custom base64 alphabet found. Unsupported file."
        )
        return None


def parse_config_value(value: str) -> bool | int | str:
    """Convert config values to the appropriate Python data types

    Args:
        value (str): The config value in string format

    Returns:
        bool|int|str: The converted value
    """
    if value == "No":
        return False
    elif value == "Yes":
        return True
    elif value.isnumeric():
        return int(value)
    else:
        return value


def get_config(strings: List[str]) -> dict:
    """Extract the configuration data from a list of extracted strings

    Args:
        strings (List[str]): A list of strings extracted from a DarkGate sample

    Returns:
        dict: Extracted configuration data. May be empty if no usable configuration data was found.
    """
    config_flag_mapping = {
        "0": "c2_port",
        "1": "startup_persistence",
        "2": "rootkit",
        "3": "anti_vm",
        "4": "min_disk",
        "5": "check_disk",
        "6": "anti_analysis",
        "7": "min_ram",
        "8": "check_ram",
        "9": "check_xeon",
        "10": "internal_mutex",
        "11": "crypter_rawstub",
        "12": "crypter_dll",
        "13": "crypter_au3",
        "15": "crypto_key",
        "16": "c2_ping_interval",
        "17": "anti_debug",
    }
    result = {}
    for string in strings:
        if "1=Yes" in string or "1=No" in string:
            for item in re.findall(r"(\d+)=(\w+)", string):
                if item[0] in config_flag_mapping:
                    result[config_flag_mapping[item[0]]] = parse_config_value(item[1])
                else:
                    result[f"flag_{item[0]}"] = parse_config_value(item[1])
        else:
            if re.match(r"^https?:\/\/", string):
                split_string = string.strip("\0").strip().split("|")
                if len(split_string) > 1:
                    split_string.remove("")
                    result["c2_servers"] = split_string
    return result


def decrypt_payload(payload: bytes, xor_key: int) -> bytes:
    """Decrypt a base64-encoded and XOR-encoded payload file

    Args:
        payload (bytes): Encrypted DarkGate payload
        xor_key (int): XOR key to use for decryption

    Returns:
        bytes: Decrypted sample
    """
    decoded = base64.b64decode(payload)
    decrypted = bytes(b ^ xor_key for b in decoded)
    return decrypted


def unpack_au3_payload(content: bytes) -> bytes:
    """Unpack the contained PE file from an AutoIt script file

    Args:
        content (bytes): The AutoIt script file content

    Returns:
        bytes: Unpacked DarkGate PE file payload. May be `None` if the unpacking fails.
    """
    try:
        splitted = content.split(b"|")
        xor_key = "a" + splitted[1][1:9].decode()
        final_xor_key = len(xor_key)
        for char in xor_key:
            final_xor_key ^= ord(char)
        final_xor_key = ~final_xor_key
        final_xor_key &= 255
        logging.info(
            f"Sample uses the following key: {xor_key}. Calculated XOR key is: 0x{final_xor_key:2x}"
        )
        return decrypt_payload(splitted[2], final_xor_key)
    except UnicodeDecodeError:
        return None
    except binascii.Error:
        return None


def find_darkgate_payload_bruteforce(content: bytes) -> bytes:
    """Try to unpack encrypted DarkGate PE payload using brute force approach with all possible single byte XOR keys

    Args:
        content (bytes): File content

    Returns:
        bytes: Unpacked DarkGate PE file payload. May be `None` if the unpacking fails.
    """
    for xor_key in range(256):
        encoded = bytes(b ^ xor_key for b in PE_START_BYTES)
        b64 = base64.b64encode(encoded)  # Look for this sequence in the payload file
        if b64 in content:
            try:
                offset = content.index(b64)
                if not offset:
                    continue
                b64_string_end = content[offset:].index(b"|")
                if not b64_string_end:
                    continue
                payload = content[offset : offset + b64_string_end]
                logging.info(
                    f"Found embedded payload file candidate with XOR key 0x{xor_key:02x} at offset {offset} with length {b64_string_end}."
                )
                return decrypt_payload(payload, xor_key)
            except binascii.Error:
                continue
            except ValueError:
                continue
    return None


def unpack_msi_wrapped_payload(filename: str) -> bytes:
    """Try to find the AU3 payload contained in a DarkGate wrapped MSI file

    Args:
        filename (str): Filename of the MSI file

    Returns:
        bytes: The AU3 file content, if found
    """
    with tempfile.TemporaryDirectory() as td:
        try:
            bin_7z = subprocess.check_output(["which", "7z"]).decode().strip()
            subprocess.check_output(
                [bin_7z, "e", f"-o{td}", filename, "Binary.bz.WrappedSetupProgram"]
            )
            subprocess.check_output(
                [
                    bin_7z,
                    "e",
                    f"-o{td}",
                    os.path.join(td, "Binary.bz.WrappedSetupProgram"),
                ]
            )
            for file in os.listdir(td):
                with open(os.path.join(td, file), "rb") as f:
                    content = f.read()
                mime_type = magic.from_buffer(content, mime=True)
                if "application/vnd.ms-cab-compressed" in mime_type:
                    logging.info("CAB file wrapped payload detected.")
                    content = unpack_cab_wrapped_payload(os.path.join(td, file))
                    return content
        except subprocess.CalledProcessError:
            logging.error("Unpacking of MSI file failed")
            return None


def unpack_cab_wrapped_payload(filename: str) -> bytes:
    """Try to find the AU3 payload contained in a DarkGate wrapped CAB file

    Args:
        filename (str): Filename of the CAB file

    Returns:
        bytes: The AU3 file content, if found
    """
    with tempfile.TemporaryDirectory() as td:
        try:
            bin_7z = subprocess.check_output(["which", "7z"]).decode().strip()
            subprocess.check_output([bin_7z, "e", f"-o{td}", filename])
            for file in os.listdir(td):
                with open(os.path.join(td, file), "rb") as f:
                    content = f.read()
                if AU3_MAGIC_BYTES in content:
                    logging.info(f"Found AU3 script in file {file} in the CAB archive.")
                    return content
        except subprocess.CalledProcessError:
            logging.error("Unpacking of CAB file failed")
            return None


def analyze_file(filename: str, include_strings=False):
    """Main function that performs the analysis of the provided file

    Args:
        filename (str): Filename of the analyzed file
        include_strings (bool, optional): Include decrypted strings in the result. Defaults to False.
    """
    logging.info(f"Performing analysis of file: {filename}")
    with open(filename, "rb") as f:
        content = f.read()
    mime_type = magic.from_buffer(content, mime=True)
    payload = None
    if (
        "application/vnd.microsoft.portable-executable" in mime_type
        and content.startswith(PE_START_BYTES)
        and b"DarkGate" in content
    ):
        logging.info("PE File detected, potentially a DarkGate sample.")
        payload = content
    elif (
        "application/x-msi" in mime_type
        and b"Wrapped using MSI Wrapper from www.exemsi.com" in content
    ):
        logging.info("MSI wrapped payload detected.")
        intermediate_payload = unpack_msi_wrapped_payload(filename)
        if intermediate_payload:
            payload = unpack_au3_payload(intermediate_payload)
            if not payload:
                payload = find_darkgate_payload_bruteforce(intermediate_payload)
    elif "application/vnd.ms-cab-compressed" in mime_type:
        logging.info("CAB file wrapped payload detected.")
        intermediate_payload = unpack_cab_wrapped_payload(filename)
        if intermediate_payload:
            payload = unpack_au3_payload(intermediate_payload)
            if not payload:
                payload = find_darkgate_payload_bruteforce(intermediate_payload)
    elif "text/plain" in mime_type and AU3_MAGIC_BYTES in content:
        logging.info(
            "Compiled AutoIt script detected, searching for embedded DarkGate payload."
        )
        payload = unpack_au3_payload(content)
        if not payload:
            payload = find_darkgate_payload_bruteforce(content)

    if payload:
        result = perform_string_extraction(payload)
        if result:
            config = get_config(result)
            if include_strings:
                config["strings"] = result
            print(json.dumps(config, sort_keys=True, indent=4))
        else:
            logging.error(f"Could not extract strings from file: {filename}")
            sys.exit(1)
    else:
        logging.error(f"No usable payload found in file: {filename}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument(
        "-s",
        "--strings",
        required=False,
        action="store_true",
        help="Output decrypted strings",
    )
    parser.add_argument(
        "-d",
        "--debug",
        required=False,
        action="store_true",
        help="Provide debug log output",
    )
    args = parser.parse_args()
    if args.debug:
        level = logging.INFO
    else:
        level = logging.ERROR
    logging.basicConfig(format="[%(levelname)s] %(message)s", level=level)
    logging.info("Starting Telekom Security DarkGate Extractor")
    analyze_file(args.file, args.strings)

