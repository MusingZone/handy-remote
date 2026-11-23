// Minisign signature verification (Ed25519, legacy and BLAKE2b-prehashed), as used by the Tauri updater.
// Self-contained so the updater needs nothing beyond Windows PowerShell 5.1 / .NET Framework.
using System;
using System.IO;
using System.Linq;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;

namespace HandyRemote
{
    public static class Minisign
    {
        // pubkey: the updater "pubkey" value from tauri.conf.json (base64 of the minisign .pub file) or the
        // bare base64 key line. sig: the content of the Tauri .sig file (base64 of the minisign .minisig file).
        // Returns the trusted comment on success; throws on any failure.
        public static string Verify(string pubkey, string sig, byte[] data)
        {
            byte[] pk = ParsePublicKey(pubkey);
            string[] lines = DecodeMaybeBase64(sig).Replace("\r", "").Split('\n');
            if (lines.Length < 4 || !lines[2].StartsWith("trusted comment: "))
                throw new InvalidDataException("malformed signature file");
            byte[] s = Convert.FromBase64String(lines[1].Trim());
            if (s.Length != 74) throw new InvalidDataException("bad signature length");
            string alg = Encoding.ASCII.GetString(s, 0, 2);
            if (!s.Skip(2).Take(8).SequenceEqual(pk.Skip(2).Take(8)))
                throw new InvalidDataException("signature was made with a different key");
            byte[] message;
            if (alg == "ED") message = Blake2b512(data);
            else if (alg == "Ed") message = data;
            else throw new InvalidDataException("unknown signature algorithm");
            byte[] edSig = s.Skip(10).ToArray();
            byte[] key = pk.Skip(10).ToArray();
            if (!Ed25519.Verify(key, message, edSig))
                throw new CryptographicException("signature does not match the file");
            string trusted = lines[2].Substring("trusted comment: ".Length);
            byte[] global = Convert.FromBase64String(lines[3].Trim());
            byte[] signed = edSig.Concat(Encoding.UTF8.GetBytes(trusted)).ToArray();
            if (!Ed25519.Verify(key, signed, global))
                throw new CryptographicException("trusted comment signature is invalid");
            return trusted;
        }

        static byte[] ParsePublicKey(string pubkey)
        {
            string text = DecodeMaybeBase64(pubkey).Replace("\r", "");
            string line = text.Split('\n').Select(l => l.Trim()).Where(l => l.Length > 0 && !l.StartsWith("untrusted comment:")).First();
            byte[] pk = Convert.FromBase64String(line);
            if (pk.Length != 42 || pk[0] != (byte)'E' || pk[1] != (byte)'d')
                throw new InvalidDataException("not a minisign Ed25519 public key");
            return pk;
        }

        static string DecodeMaybeBase64(string s)
        {
            s = s.Trim();
            if (s.StartsWith("untrusted comment:")) return s;
            return Encoding.UTF8.GetString(Convert.FromBase64String(s));
        }

        // ---- BLAKE2b-512 (RFC 7693), unkeyed ----
        static readonly ulong[] IV = {
            0x6a09e667f3bcc908UL, 0xbb67ae8584caa73bUL, 0x3c6ef372fe94f82bUL, 0xa54ff53a5f1d36f1UL,
            0x510e527fade682d1UL, 0x9b05688c2b3e6c1fUL, 0x1f83d9abfb41bd6bUL, 0x5be0cd19137e2179UL };
        static readonly byte[,] SIGMA = {
            {0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15}, {14,10,4,8,9,15,13,6,1,12,0,2,11,7,5,3},
            {11,8,12,0,5,2,15,13,10,14,3,6,7,1,9,4}, {7,9,3,1,13,12,11,14,2,6,5,10,4,0,15,8},
            {9,0,5,7,2,4,10,15,14,1,11,12,6,8,3,13}, {2,12,6,10,0,11,8,3,4,13,7,5,15,14,1,9},
            {12,5,1,15,14,13,4,10,0,7,6,3,9,2,8,11}, {13,11,7,14,12,1,3,9,5,0,15,4,8,6,2,10},
            {6,15,14,9,11,3,0,8,12,2,13,7,1,4,10,5}, {10,2,8,4,7,6,1,5,15,11,9,14,3,12,13,0} };

        static ulong Rotr(ulong x, int n) { return (x >> n) | (x << (64 - n)); }

        static void Compress(ulong[] h, byte[] block, ulong t, bool last)
        {
            ulong[] m = new ulong[16];
            for (int i = 0; i < 16; i++) m[i] = BitConverter.ToUInt64(block, i * 8);
            ulong[] v = new ulong[16];
            Array.Copy(h, v, 8);
            Array.Copy(IV, 0, v, 8, 8);
            v[12] ^= t;
            if (last) v[14] = ~v[14];
            for (int r = 0; r < 12; r++)
            {
                int s = r % 10;
                G(v, 0, 4, 8, 12, m[SIGMA[s, 0]], m[SIGMA[s, 1]]);
                G(v, 1, 5, 9, 13, m[SIGMA[s, 2]], m[SIGMA[s, 3]]);
                G(v, 2, 6, 10, 14, m[SIGMA[s, 4]], m[SIGMA[s, 5]]);
                G(v, 3, 7, 11, 15, m[SIGMA[s, 6]], m[SIGMA[s, 7]]);
                G(v, 0, 5, 10, 15, m[SIGMA[s, 8]], m[SIGMA[s, 9]]);
                G(v, 1, 6, 11, 12, m[SIGMA[s, 10]], m[SIGMA[s, 11]]);
                G(v, 2, 7, 8, 13, m[SIGMA[s, 12]], m[SIGMA[s, 13]]);
                G(v, 3, 4, 9, 14, m[SIGMA[s, 14]], m[SIGMA[s, 15]]);
            }
            for (int i = 0; i < 8; i++) h[i] ^= v[i] ^ v[i + 8];
        }

        static void G(ulong[] v, int a, int b, int c, int d, ulong x, ulong y)
        {
            v[a] = v[a] + v[b] + x; v[d] = Rotr(v[d] ^ v[a], 32);
            v[c] = v[c] + v[d]; v[b] = Rotr(v[b] ^ v[c], 24);
            v[a] = v[a] + v[b] + y; v[d] = Rotr(v[d] ^ v[a], 16);
            v[c] = v[c] + v[d]; v[b] = Rotr(v[b] ^ v[c], 63);
        }

        public static byte[] Blake2b512(byte[] data)
        {
            ulong[] h = (ulong[])IV.Clone();
            h[0] ^= 0x01010000UL ^ 64UL;
            byte[] block = new byte[128];
            int off = 0;
            ulong t = 0;
            while (data.Length - off > 128)
            {
                Buffer.BlockCopy(data, off, block, 0, 128);
                t += 128;
                Compress(h, block, t, false);
                off += 128;
            }
            Array.Clear(block, 0, 128);
            int rem = data.Length - off;
            Buffer.BlockCopy(data, off, block, 0, rem);
            t += (ulong)rem;
            Compress(h, block, t, true);
            byte[] outp = new byte[64];
            for (int i = 0; i < 8; i++) Buffer.BlockCopy(BitConverter.GetBytes(h[i]), 0, outp, i * 8, 8);
            return outp;
        }
    }

    // Ed25519 verification (RFC 8032) in extended coordinates; speed is irrelevant here, correctness is not.
    public static class Ed25519
    {
        static readonly BigInteger P = BigInteger.Pow(2, 255) - 19;
        static readonly BigInteger L = BigInteger.Pow(2, 252) + BigInteger.Parse("27742317777372353535851937790883648493");
        static readonly BigInteger D = Mod(-121665 * Inv(121666));
        static readonly BigInteger SqrtM1 = BigInteger.ModPow(2, (P - 1) / 4, P);
        static readonly BigInteger[] B = MakeBase();

        static BigInteger Mod(BigInteger a) { BigInteger r = a % P; return r.Sign < 0 ? r + P : r; }
        static BigInteger Inv(BigInteger a) { return BigInteger.ModPow(Mod(a), P - 2, P); }

        static BigInteger[] MakeBase()
        {
            BigInteger y = Mod(4 * Inv(5));
            BigInteger x = RecoverX(y, false);
            return new[] { x, y, BigInteger.One, Mod(x * y) };
        }

        static BigInteger RecoverX(BigInteger y, bool sign)
        {
            if (y >= P) throw new ArgumentException("y out of range");
            BigInteger x2 = Mod((y * y - 1) * Inv(D * y * y + 1));
            if (x2.IsZero) { if (sign) throw new ArgumentException("bad point"); return BigInteger.Zero; }
            BigInteger x = BigInteger.ModPow(x2, (P + 3) / 8, P);
            if (Mod(x * x - x2) != 0) x = Mod(x * SqrtM1);
            if (Mod(x * x - x2) != 0) throw new ArgumentException("not on curve");
            if (x.IsEven == sign) x = P - x;
            return x;
        }

        static BigInteger[] Add(BigInteger[] p, BigInteger[] q)
        {
            BigInteger a = Mod((p[1] - p[0]) * (q[1] - q[0]));
            BigInteger b = Mod((p[1] + p[0]) * (q[1] + q[0]));
            BigInteger c = Mod(2 * p[3] * q[3] * D);
            BigInteger d = Mod(2 * p[2] * q[2]);
            BigInteger e = b - a, f = d - c, g = d + c, h = b + a;
            return new[] { Mod(e * f), Mod(g * h), Mod(f * g), Mod(e * h) };
        }

        static BigInteger[] Mul(BigInteger s, BigInteger[] p)
        {
            BigInteger[] q = { BigInteger.Zero, BigInteger.One, BigInteger.One, BigInteger.Zero };
            while (s > 0)
            {
                if (!s.IsEven) q = Add(q, p);
                p = Add(p, p);
                s >>= 1;
            }
            return q;
        }

        static bool Equal(BigInteger[] p, BigInteger[] q)
        {
            return Mod(p[0] * q[2] - q[0] * p[2]) == 0 && Mod(p[1] * q[2] - q[1] * p[2]) == 0;
        }

        static BigInteger FromLE(byte[] b, int off, int len)
        {
            byte[] t = new byte[len + 1];
            Buffer.BlockCopy(b, off, t, 0, len);
            return new BigInteger(t);
        }

        static BigInteger[] Decode(byte[] b, int off)
        {
            byte[] t = new byte[32];
            Buffer.BlockCopy(b, off, t, 0, 32);
            bool sign = (t[31] & 0x80) != 0;
            t[31] &= 0x7f;
            BigInteger y = FromLE(t, 0, 32);
            BigInteger x = RecoverX(y, sign);
            return new[] { x, y, BigInteger.One, Mod(x * y) };
        }

        public static bool Verify(byte[] publicKey, byte[] message, byte[] signature)
        {
            if (publicKey.Length != 32 || signature.Length != 64) return false;
            BigInteger[] a, r;
            try { a = Decode(publicKey, 0); r = Decode(signature, 0); }
            catch (ArgumentException) { return false; }
            BigInteger s = FromLE(signature, 32, 32);
            if (s >= L) return false;
            byte[] hash;
            using (SHA512 sha = SHA512.Create())
                hash = sha.ComputeHash(signature.Take(32).Concat(publicKey).Concat(message).ToArray());
            BigInteger h = FromLE(hash, 0, 64) % L;
            return Equal(Mul(s, B), Add(r, Mul(h, a)));
        }
    }
}
