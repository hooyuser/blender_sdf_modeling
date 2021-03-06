import bmesh
import taichi as ti
import numblend as nb
from math import sqrt

nb.init()
ti.init(arch=ti.cpu, debug=False)

coll_r = 1.01
k_stretch = 0.9
k_bend = 0.7
k_LRA = 0.5
tether_give = 0.2
eps = 1e-3


@ti.data_oriented
class TiClothSimulation:
    def __init__(self, sdf_phy, frame_end):
        self.dt = sdf_phy.time_step / 1000.0
        self.device = sdf_phy.device
        self.substep_num = sdf_phy.substep_num
        self.solver_num = sdf_phy.solver_num
        self.drag_damping = sdf_phy.drag_damping
        self.enable_LRA = sdf_phy.enable_LRA
        self.frame_num = frame_end
        self.obj = sdf_phy.cloth_obj
        self.me = self.obj.data
        self.pin_group = self.obj.vertex_groups[sdf_phy.pin_group]
        self.pin_index = self.pin_group.index
        self.c_obj = sdf_phy.c_obj

        self.bm = bmesh.new()  # create an empty BMesh
        self.bm.from_mesh(self.me)  # fill it in from a Mesh
        self.vertex_num = len(self.bm.verts)
        self.edge_num = len(self.bm.edges)
        self.face_num = len(self.bm.faces)
        self.link_num = self.edge_num + self.face_num * 2

        # vertex position
        self.x = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_num)

        # predicted vertex position
        self.p = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_num)

        # vertex position
        self.v = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_num)

        # w = 1/m, the reciprocal of mass
        self.w = ti.field(dtype=ti.f32, shape=self.vertex_num)

        # vertex i and vertex j are linked if (i,j) in self.link
        self.link = ti.Vector.field(2, dtype=ti.i32, shape=self.link_num)

        # the distance between vertex i and vertex j
        self.link_len = ti.field(dtype=ti.f32, shape=self.link_num)
        self.link_k = ti.field(dtype=ti.f32, shape=self.link_num)
        self.link_idx = ti.field(dtype=ti.i32, shape=())
        self.coll_origin = ti.Vector.field(3, dtype=ti.f32, shape=1)

        # Long Range Attachments
        if self.enable_LRA:
            self.attach_index = self.obj.vertex_groups[
                sdf_phy.attach_group].index

            # indexes of attachment points
            self.attach = [
                v.index for v in self.me.vertices
                if self.attach_index in [vg.group for vg in v.groups]
            ]

            self.attach_num = len(self.attach)

            # positions of of attachment points
            self.attach_pos = ti.Vector.field(3,
                                              dtype=ti.f32,
                                              shape=self.attach_num)
            self.tether_len = ti.field(dtype=ti.f32,
                                       shape=(self.vertex_num,
                                              self.attach_num))

        self.initialize()

    @staticmethod
    def calc_dist(first, second):
        locx = second[0] - first[0]
        locy = second[1] - first[1]
        locz = second[2] - first[2]

        distance = sqrt((locx)**2 + (locy)**2 + (locz)**2)
        return distance

    def set_faces(self):
        for i in range(self.face_num):
            v = self.bm.faces[i].verts

            for j in ti.static(range(2)):
                self.link[self.link_idx[None]] = ti.Vector(
                    [v[0 + j].index, v[1 + j].index])
                self.link_len[self.link_idx[None]] = self.calc_dist(
                    v[0 + j].co, v[1 + j].co)
                self.link_k[self.link_idx[None]] = k_bend
                self.link_idx[None] += 1

    def set_edges(self):
        for i in range(self.edge_num):
            e = self.bm.edges[self.link_idx[None]]
            self.link[self.link_idx[None]] = ti.Vector(
                [e.verts[0].index, e.verts[1].index])
            self.link_len[self.link_idx[None]] = e.calc_length()
            self.link_k[self.link_idx[None]] = k_stretch
            self.link_idx[None] += 1

    def set_attachments(self):
        for i in range(self.attach_num):
            self.attach_pos[i] = ti.Vector(
                list(self.bm.verts[self.attach[i]].co))
            # print(attach_pos[i].x,attach_pos[i].y,attach_pos[i].z)

    def initialize(self):
        self.bm.verts.ensure_lookup_table()
        self.bm.edges.ensure_lookup_table()
        self.bm.faces.ensure_lookup_table()

        if self.enable_LRA:
            self.set_attachments()

        for i in range(self.vertex_num):
            self.x[i] = ti.Vector(list(self.bm.verts[i].co))
            self.w[i] = 1
            for g in self.me.vertices[i].groups:
                if g.group == self.pin_index:
                    self.w[i] = 1 - self.pin_group.weight(i)

            if self.enable_LRA:
                for att in range(self.attach_num):
                    self.tether_len[i, att] = self.calc_dist(
                        self.x[i], self.attach_pos[att])

                # print(i,att,': ', tether_len[i,att])
            # print(i,': ',w[i],attach[i])

        self.link_idx[None] = 0
        self.set_edges()
        self.set_faces()

    @ti.kernel
    def substep_cpu(self):
        for i in range(self.vertex_num):
            self.v[i] += self.dt * ti.Vector([0, 0, -9.8]) * self.w[i]
            self.v[i] *= ti.exp(-self.dt * self.drag_damping)
            self.p[i] = self.x[i] + self.dt * self.v[i]

        for n in range(self.solver_num):
            if ti.static(self.enable_LRA):
                for vi in range(self.vertex_num):
                    for att in range(self.attach_num):
                        if self.w[vi] > eps:
                            dist = (self.p[vi] - self.attach_pos[att]).norm()
                            dist_diff = dist - self.tether_len[vi, att]
                            if dist_diff / self.tether_len[vi,
                                                           att] > tether_give:
                                dp = -0.5 * self.w[vi] * dist_diff * (
                                    self.p[vi] - self.attach_pos[att]) / dist
                                self.p[vi] += k_LRA * dp
            #    if w[vi] > eps and dist - coll_r < 0:
            # print(attach[0])

            for l in range(self.link_num):
                p0 = self.link[l][0]
                p1 = self.link[l][1]
                p_01 = self.p[p0] - self.p[p1]

                kp = 1 - pow((1 - self.link_k[l]), 1 / (n + 1))

                length = self.link_len[l]
                if self.w[p0] > eps:
                    dp0 = -0.5 * self.w[p0] * (p_01.norm() -
                                               length) * p_01.normalized()
                    self.p[self.link[l][0]] += kp * dp0

                if self.w[p1] > eps:
                    dp1 = 0.5 * self.w[p1] * (p_01.norm() -
                                              length) * p_01.normalized()
                    self.p[self.link[l][1]] += kp * dp1

            for vi in range(self.vertex_num):
                if self.w[vi] > eps:
                    dist = (self.p[vi] - self.coll_origin[0]).norm()
                    dist_diff = dist - coll_r
                    if dist_diff < 0:
                        dp = -dist_diff / dist * (self.p[vi] -
                                                  self.coll_origin[0])
                        self.p[vi] += dp

        for i in range(self.vertex_num):
            # print('p[',i,']:', p[i])
            self.v[i] = (self.p[i] - self.x[i]) / self.dt
            self.x[i] = self.p[i]

    @ti.kernel
    def substep_gpu(self):
        for i in range(self.vertex_num):
            self.v[i] += self.dt * ti.Vector([0, 0, -9.8]) * self.w[i]
            self.v[i] *= ti.exp(-self.dt * self.drag_damping)
            self.p[i] = self.x[i] + self.dt * self.v[i]

        for n in ti.static(range(self.solver_num)):
            if ti.static(self.enable_LRA):
                for vi in range(self.vertex_num):
                    for att in range(self.attach_num):
                        if self.w[vi] > eps:
                            dist = (self.p[vi] - self.attach_pos[att]).norm()
                            dist_diff = dist - self.tether_len[vi, att]
                            if dist_diff / self.tether_len[vi,
                                                           att] > tether_give:
                                dp = -0.5 * self.w[vi] * dist_diff * (
                                    self.p[vi] - self.attach_pos[att]) / dist
                                self.p[vi] += k_LRA * dp
            #    if w[vi] > eps and dist - coll_r < 0:
            # print(attach[0])

            for l in range(self.link_num):
                p0 = self.link[l][0]
                p1 = self.link[l][1]
                p_01 = self.p[p0] - self.p[p1]

                kp = 1 - pow((1 - self.link_k[l]), 1 / (n + 1))

                length = self.link_len[l]
                if self.w[p0] > eps:
                    dp0 = -0.5 * self.w[p0] * (p_01.norm() -
                                               length) * p_01.normalized()
                    self.p[self.link[l][0]] += kp * dp0

                if self.w[p1] > eps:
                    dp1 = 0.5 * self.w[p1] * (p_01.norm() -
                                              length) * p_01.normalized()
                    self.p[self.link[l][1]] += kp * dp1

            for vi in range(self.vertex_num):
                if self.w[vi] > eps:
                    dist = (self.p[vi] - self.coll_origin[0]).norm()
                    dist_diff = dist - coll_r
                    if dist_diff < 0:
                        dp = -dist_diff / dist * (self.p[vi] -
                                                  self.coll_origin[0])
                        self.p[vi] += dp

        for i in range(self.vertex_num):
            # print('p[',i,']:', p[i])
            self.v[i] = (self.p[i] - self.x[i]) / self.dt
            self.x[i] = self.p[i]

    def animate(self):
        substep = self.substep_cpu if self.device == 'CPU' else self.substep_gpu

        @nb.add_animation
        def main():
            for frame in range(self.frame_num):
                yield nb.mesh_update(
                    self.me,
                    self.x.to_numpy().reshape(self.vertex_num, 3))
                # s = 1
                for step in range(self.substep_num):
                    # print('frame:',frame,', substep:',s)
                    # s += 1
                    self.coll_origin[0].x = self.c_obj.location[0]
                    self.coll_origin[0].y = self.c_obj.location[1]
                    self.coll_origin[0].z = self.c_obj.location[2]
                    substep()
