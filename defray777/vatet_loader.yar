rule Vatet_Loader_Rufus_Backdoor : defray777
{
	meta:
        author = "Thomas Barabosch, Deutsche Telekom Security"
		date = "2022-03-18"
        description = "Detects backdoored Rufus with Vatet Loader of Defray777"
        reference1 = "https://github.com/pbatard/rufus"
        reference2 = "https://unit42.paloaltonetworks.com/vatet-pyxie-defray777"
	strings:
        /*
            0x4d0714 660FF8C1                      psubb xmm0, xmm1
	        0x4d0718 660FEFC2                      pxor xmm0, xmm2
	        0x4d071c 660FF8C1                      psubb xmm0, xmm1
	    */
		$payload_decryption = { 66 0F F8 C1 66 0F EF C2 66 0F F8 C1 }
        $mz = "MZ" ascii
        $rufus = "https://rufus.ie/" ascii
	condition:
		$mz at 0
        and $payload_decryption
        and $rufus
}
